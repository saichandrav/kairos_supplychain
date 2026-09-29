"""A4 Resilience Simulator: the cheapest action that keeps the hospital covered through the risk
window without letting stock expire (plan §3 A4).

Options per at-risk formulation (from the A3 Forecast):
  BUFFER           extra stock from the current supplier
  ALT_SUPPLIER     another manufacturer of the same formulation (FF_REF_PRODUCER)
  THERAPEUTIC_ALT  a formulation in the same THERAPEUTIC_CLASS; always needs clinical review (A5)
  REBALANCE        move stock between locations (FF_MM_MCHB WERKS/LGORT) so no location runs out first

Each purchase option is sized with OR-Tools CP-SAT:
  minimise  rate·q + stockout_penalty·u      (paise, integers)
  s.t.      q + u >= shortfall               shortfall = daily × horizon − usable stock
            q = 0 or moq <= q <= expiry_safe  expiry_safe = daily × (shelf life − margin) − usable stock
            rate·q <= budget, q <= free storage (cold-chain or ambient)
Correlated-risk check: an alternate that shares an API origin country or KSM with the current
supply (a2_dependency.shares_upstream) is rejected (rank None) or penalised, per rules.

Rates are GST-inclusive (what the hospital pays); PO net prices are grossed up by GST.
Writes FF_AG_SCENARIO (S01 columns).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

import networkx as nx
import pandas as pd
from ortools.sat.python import cp_model

from agents import a2_dependency as a2
from agents.common import AgentCtx, placeholders, resolve_ctx, try_query
from agents.contracts import Forecast, OptionType, Scenario
from agents.ctx import Db
from agents.rules import Rules

AGENT = "A4"
DAYS_PER_MONTH = 30.44
ISSUE_MOVEMENTS = ("201", "261")
SCENARIO_COLS = (
    "RUN_ID", "SCENARIO_ID", "FORM_ID", "OPTION_TYPE", "SUPPLIER", "MATERIAL", "QTY", "UNIT_RATE_INR",
    "COST_INR", "COVERAGE_DAYS", "EXPIRY_WASTE_RISK", "CORRELATED_RISK_FLAG", "RANK_NO", "CREATED_AT",
)


def _w(rules: Rules, path: str) -> Any:
    return rules.value("weights", f"resilience.{path}")


def _money(x: float) -> Decimal:
    return Decimal(f"{x:.4f}")


# ---------------------------------------------------------------- inputs


@dataclass
class Location:
    werks: str
    lgort: str
    usable: float
    daily: float

    @property
    def cover(self) -> float:
        return self.usable / self.daily if self.daily > 0 else math.inf


@dataclass
class Option:
    option_type: OptionType
    supplier: str | None
    material: str | None
    rate: float  # GST-inclusive INR per unit
    substitute_formulation_id: str | None = None
    correlated: bool = False
    correlated_reason: str | None = None


@dataclass
class Situation:
    """Everything A4 needs about one at-risk formulation."""

    form_id: str
    as_of: date
    material: str | None
    plant: str | None
    cold_chain: bool
    daily: float
    usable: float
    horizon_days: float
    ceiling_net: float | None
    gst: float
    current_supplier: str | None
    options: list[Option] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)

    @property
    def shortfall(self) -> int:
        return max(0, math.ceil(self.daily * self.horizon_days - self.usable))


def _ceiling(db: Db, form_id: str, as_of: date, rules: Rules) -> tuple[float | None, float]:
    cp = try_query(db, "SELECT EFFECTIVE_FROM, CEILING_PRICE, GST_RATE FROM FF_REF_CEILING_PRICE WHERE FORM_ID = ?",
                   (form_id,))
    return _ceiling_of(cp, as_of, rules)


def _ceiling_of(cp: pd.DataFrame | None, as_of: date, rules: Rules) -> tuple[float | None, float]:
    """(ceiling, GST) in force on as_of from one formulation's FF_REF_CEILING_PRICE rows."""
    default_gst = float(rules.value("dpco", "constants.gst_rate_medicines"))
    if cp is None or cp.empty:
        return None, default_gst
    cp = cp[pd.to_datetime(cp["EFFECTIVE_FROM"].astype(str)).dt.date <= as_of].sort_values("EFFECTIVE_FROM")
    if cp.empty:
        return None, default_gst
    r = cp.iloc[-1]
    gst = r["GST_RATE"] if r["GST_RATE"] is not None and not pd.isna(r["GST_RATE"]) else default_gst
    return float(r["CEILING_PRICE"]), float(gst)


def _last_po(db: Db, material: str, vendor: str | None = None) -> tuple[str | None, float | None]:
    """(vendor, net unit price) of the latest PO line for a material, optionally for one vendor."""
    sql = ("SELECT h.LIFNR, h.BEDAT, p.NETPR, p.PEINH FROM FF_MM_EKPO p JOIN FF_MM_EKKO h ON h.EBELN = p.EBELN "
           "WHERE p.MATNR = ?")
    params: tuple = (material,)
    if vendor:
        sql, params = sql + " AND h.LIFNR = ?", (material, vendor)
    po = try_query(db, sql, params)
    if po is None or po.empty or po["NETPR"].isna().all():
        return None, None
    r = po.dropna(subset=["NETPR"]).sort_values("BEDAT").iloc[-1]
    per = float(r["PEINH"]) if r["PEINH"] is not None and not pd.isna(r["PEINH"]) and float(r["PEINH"]) > 0 else 1.0
    return str(r["LIFNR"]), float(r["NETPR"]) / per


def _producers(db: Db, form_id: str) -> list[str]:
    return _producers_of(db.query("SELECT MANUFACTURER_ID, MARKET_SHARE FROM FF_REF_PRODUCER WHERE FORM_ID = ?",
                                  (form_id,)))


def _producers_of(p: pd.DataFrame) -> list[str]:
    """Manufacturer ids by market share (desc), then id."""
    p = p.assign(S=pd.to_numeric(p["MARKET_SHARE"], errors="coerce").fillna(0.0))
    return p.sort_values(["S", "MANUFACTURER_ID"], ascending=[False, True])["MANUFACTURER_ID"].astype(str).tolist()


def _locations(db: Db, material: str, as_of: date, total_daily: float) -> list[Location]:
    stock = db.query("SELECT WERKS, LGORT, CLABS, VFDAT FROM FF_MM_MCHB WHERE MATNR = ?", (material,))
    stock = stock[pd.to_datetime(stock["VFDAT"].astype(str)).dt.date > as_of]
    usable = stock.assign(Q=stock["CLABS"].astype(float)).groupby(["WERKS", "LGORT"])["Q"].sum()
    issues = try_query(db, "SELECT WERKS, LGORT, BWART, MENGE FROM FF_MM_MSEG WHERE MATNR = ?", (material,))
    share = pd.Series(dtype=float)
    if issues is not None and len(issues):
        gi = issues[issues["BWART"].astype(str).isin(ISSUE_MOVEMENTS)]
        by = gi.assign(Q=gi["MENGE"].astype(float)).groupby(["WERKS", "LGORT"])["Q"].sum()
        share = by / by.sum() if by.sum() > 0 else share
    keys = sorted(set(usable.index) | set(share.index))
    return [Location(str(w), str(l), float(usable.get((w, l), 0.0)), total_daily * float(share.get((w, l), 0.0)))
            for w, l in keys]


def load_situation(db: Db, fc: Forecast, as_of: date, rules: Rules) -> Situation | None:
    """Build the Situation from the A3 forecast and the MM/reference tables. None if not stocked."""
    facts = fc.cause_facts
    mats = (facts.get("hospital_materials") or {}).get("value") or []
    daily = (facts.get("daily_consumption") or {}).get("value")
    if not mats or not daily:
        return None
    material = sorted(mats)[0]
    usable = float((facts.get("usable_stock_qty") or {}).get("value") or 0.0)
    mara = db.query("SELECT RAUBE FROM FF_MM_MARA WHERE MATNR = ?", (material,))
    cold = bool(len(mara)) and str(mara.iloc[0]["RAUBE"] or "").upper().startswith("COLD")
    plants = db.query("SELECT WERKS, SUM(CLABS) AS Q FROM FF_MM_MCHB WHERE MATNR = ? GROUP BY WERKS ORDER BY Q DESC",
                      (material,))
    plant = str(plants.iloc[0]["WERKS"]) if len(plants) else None

    hi = fc.window_months_hi
    horizon = (min(float(_w(rules, "max_horizon_days")), hi * DAYS_PER_MONTH) if hi is not None
               else float(_w(rules, "default_horizon_days")))
    ceiling, gst = _ceiling(db, fc.formulation_id, as_of, rules)
    vendor, net = _last_po(db, material)
    producers = _producers(db, fc.formulation_id)
    current = vendor or (producers[0] if producers else None)
    if net is None:
        net = ceiling  # no PO history: assume the ceiling (conservative)
    s = Situation(fc.formulation_id, as_of, material, plant, cold, float(daily), usable, horizon, ceiling, gst, current,
                  locations=_locations(db, material, as_of, float(daily)))
    if net is not None:
        s.options.append(Option(OptionType.BUFFER, current, material, net * (1 + gst)))
        premium = float(_w(rules, "alt_price_premium"))
        for m in producers:
            if m == current:
                continue
            _, alt_net = _last_po(db, material, m)
            s.options.append(Option(OptionType.ALT_SUPPLIER, m, material,
                                    (alt_net if alt_net is not None else net * (1 + premium)) * (1 + gst)))
    s.options.extend(_therapeutic(db, fc.formulation_id, as_of, rules))
    return s


def _therapeutic(db: Db, form_id: str, as_of: date, rules: Rules) -> list[Option]:
    cls = try_query(db, "SELECT THERAPEUTIC_CLASS FROM FF_REF_FORMULATION WHERE FORM_ID = ?", (form_id,))
    if cls is None or cls.empty or not cls.iloc[0]["THERAPEUTIC_CLASS"]:
        return []
    alts = db.query("SELECT FORM_ID FROM FF_REF_FORMULATION WHERE THERAPEUTIC_CLASS = ? AND FORM_ID <> ? "
                    "ORDER BY FORM_ID", (cls.iloc[0]["THERAPEUTIC_CLASS"], form_id))
    ids = alts["FORM_ID"].astype(str).tolist()
    if not ids:
        return []
    # one query per table for the whole class instead of three round trips per alternate
    marks = placeholders(ids)
    mara = db.query(f"SELECT FORM_ID, MATNR FROM FF_MM_MARA WHERE FORM_ID IN ({marks}) ORDER BY MATNR", ids)
    cps = try_query(db, "SELECT FORM_ID, EFFECTIVE_FROM, CEILING_PRICE, GST_RATE FROM FF_REF_CEILING_PRICE "
                        f"WHERE FORM_ID IN ({marks})", ids)
    prods = db.query(f"SELECT FORM_ID, MANUFACTURER_ID, MARKET_SHARE FROM FF_REF_PRODUCER WHERE FORM_ID IN ({marks})", ids)
    first_mat = mara.assign(F=mara["FORM_ID"].astype(str)).groupby("F")["MATNR"].first()
    cp_by = {} if cps is None else {k: g for k, g in cps.groupby(cps["FORM_ID"].astype(str))}
    prod_by = {k: g for k, g in prods.groupby(prods["FORM_ID"].astype(str))}
    empty_prods = prods.iloc[0:0]
    out = []
    for alt in ids:
        alt_mat = str(first_mat[alt]) if alt in first_mat.index else None
        vendor, net = _last_po(db, alt_mat) if alt_mat else (None, None)
        ceiling, gst = _ceiling_of(cp_by.get(alt), as_of, rules)
        net = net if net is not None else ceiling
        producers = _producers_of(prod_by.get(alt, empty_prods))
        supplier = vendor or (producers[0] if producers else None)
        if net is None or supplier is None:
            continue
        out.append(Option(OptionType.THERAPEUTIC_ALT, supplier, alt_mat, net * (1 + gst), substitute_formulation_id=alt))
    return out


# ---------------------------------------------------------------- correlated-risk check


def flag_correlated(s: Situation, g: nx.DiGraph, ctx: AgentCtx) -> None:
    """Mark alternates that share an API origin country or KSM with the current supply."""
    for o in s.options:
        if o.option_type not in (OptionType.ALT_SUPPLIER, OptionType.THERAPEUTIC_ALT):
            continue
        base = s.form_id
        if o.option_type is OptionType.ALT_SUPPLIER and s.current_supplier and \
                f"MANUFACTURER:{s.current_supplier}" in g:
            base = s.current_supplier
        other = o.substitute_formulation_id if o.option_type is OptionType.THERAPEUTIC_ALT else o.supplier
        try:
            shared = a2.shares_upstream(base, other, ctx, graph=g)
        except KeyError:
            continue
        if shared:
            parts = ([f"origin {', '.join(shared.countries)}"] if shared.countries else []) + \
                    ([f"KSM {', '.join(shared.ksms)}"] if shared.ksms else [])
            o.correlated = True
            o.correlated_reason = f"shares {' and '.join(parts)} with the current supply of {s.form_id}"


# ---------------------------------------------------------------- sizing (CP-SAT)


def expiry_safe_qty(s: Situation, rules: Rules) -> int:
    shelf_days = float(_w(rules, "offered_shelf_life_months")) * DAYS_PER_MONTH
    margin = float(_w(rules, "expiry_margin_days"))
    return max(0, math.floor(s.daily * (shelf_days - margin) - s.usable))


@dataclass
class Sized:
    qty: int
    unmet: int
    objective: float  # INR


def size_option(s: Situation, o: Option, rules: Rules) -> Sized:
    """CP-SAT: minimise purchase cost + stock-out penalty for one option under all constraints."""
    need = s.shortfall
    cap = expiry_safe_qty(s, rules)
    storage = float(rules.param("STORAGE", "cold_free_capacity_units" if s.cold_chain else "ambient_free_capacity_units"))
    budget = float(rules.param("BUDGET", "budget_remaining_inr"))
    moq = int(_w(rules, "default_moq_units"))
    rate_p = max(1, round(o.rate * 100))
    pen_p = round(float(_w(rules, "stockout_penalty_multiplier")) * (s.ceiling_net or o.rate) * 100)
    ub = int(max(0, min(cap, storage, budget * 100 // rate_p)))

    m = cp_model.CpModel()
    q = m.NewIntVar(0, ub, "q")
    u = m.NewIntVar(0, need, "u")
    y = m.NewBoolVar("order")
    m.Add(q >= moq).OnlyEnforceIf(y)
    m.Add(q == 0).OnlyEnforceIf(y.Not())
    m.Add(q + u >= need)
    m.Add(rate_p * q <= int(budget * 100))
    uplift = 1 + float(_w(rules, "correlated_risk_penalty")) if o.correlated else 1.0
    m.Minimize(rate_p * q + pen_p * u)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5.0
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Sized(0, need, pen_p * need / 100 * uplift)
    qv, uv = int(solver.Value(q)), int(solver.Value(u))
    return Sized(qv, uv, (rate_p * qv + pen_p * uv) / 100 * uplift)


def rebalance(s: Situation, rules: Rules) -> tuple[int, float] | None:
    """Units to move so the lowest-cover location reaches the hospital-average cover. None if not useful."""
    locs = [l for l in s.locations if l.daily > 0]
    if len(locs) < 2 or s.daily <= 0:
        return None
    avg = sum(l.usable for l in locs) / sum(l.daily for l in locs)
    worst = min(locs, key=lambda l: l.cover)
    need = math.ceil(avg * worst.daily - worst.usable)
    surplus = sum(max(0.0, l.usable - avg * l.daily) for l in locs if l is not worst)
    qty = int(min(need, math.floor(surplus)))
    if qty <= 0:
        return None
    return qty, (worst.usable + qty) / worst.daily


# ---------------------------------------------------------------- run


def _scenario(s: Situation, idx: int, run_id: str, o: Option, qty: int, rules: Rules) -> Scenario:
    total = s.usable + qty
    shelf_m = float(_w(rules, "offered_shelf_life_months"))
    wasted = max(0.0, total - s.daily * shelf_m * DAYS_PER_MONTH)
    return Scenario(
        scenario_id=f"{run_id}:{s.form_id}:{idx:02d}",
        run_id=run_id,
        formulation_id=s.form_id,
        option_type=o.option_type,
        supplier=o.supplier,
        material=o.material,
        qty=qty,
        unit_rate_inr=_money(o.rate),
        cost=_money(o.rate * qty),
        coverage_days=round(total / s.daily, 4) if s.daily > 0 else 0.0,
        expiry_waste_risk=round(min(1.0, wasted / qty), 6) if qty else 0.0,
        correlated_risk_flag=o.correlated,
        correlated_risk_reason=o.correlated_reason,
        plant=s.plant,
        cold_chain=s.cold_chain,
        residual_shelf_life_months=shelf_m,
        substitute_formulation_id=o.substitute_formulation_id,
    )


def scenarios_for(s: Situation, run_id: str, ctx: AgentCtx, graph: nx.DiGraph) -> list[Scenario]:
    rules = ctx.rules
    flag_correlated(s, graph, ctx)
    reject = str(_w(rules, "correlated_risk_action")).lower() == "reject"
    scored: list[tuple[float, Scenario]] = []
    rejected: list[Scenario] = []
    idx = 0
    for o in s.options:
        sized = size_option(s, o, rules)
        if sized.qty == 0:
            continue  # infeasible under MOQ / expiry / budget / storage
        idx += 1
        sc = _scenario(s, idx, run_id, o, sized.qty, rules)
        (rejected.append(sc) if o.correlated and reject else scored.append((sized.objective, sc)))
    rb = rebalance(s, rules)
    if rb:
        qty, cover = rb
        idx += 1
        handling = float(_w(rules, "rebalance_handling_inr_per_unit"))
        pen = float(_w(rules, "stockout_penalty_multiplier")) * (s.ceiling_net or 0.0)
        sc = Scenario(scenario_id=f"{run_id}:{s.form_id}:{idx:02d}", run_id=run_id, formulation_id=s.form_id,
                      option_type=OptionType.REBALANCE, material=s.material, qty=qty, unit_rate_inr=None,
                      cost=_money(handling * qty), coverage_days=round(cover, 4), plant=s.plant,
                      cold_chain=s.cold_chain)
        scored.append((handling * qty + pen * s.shortfall, sc))  # doesn't add stock: full shortfall remains
    scored.sort(key=lambda t: (t[0], t[1].scenario_id))
    ranked = [sc.model_copy(update={"rank": i}) for i, (_, sc) in enumerate(scored, start=1)]
    return ranked + rejected


def _write(db: Db, scenarios: list[Scenario]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    num = (lambda d: None if d is None else (float(d) if db.backend == "sqlite" else d))
    rows = [(s.run_id, s.scenario_id, s.formulation_id, s.option_type.value, s.supplier, s.material, s.qty,
             num(s.unit_rate_inr), num(s.cost), s.coverage_days, s.expiry_waste_risk, int(s.correlated_risk_flag),
             s.rank, now) for s in scenarios]
    db.executemany(f"INSERT INTO FF_AG_SCENARIO ({', '.join(SCENARIO_COLS)}) VALUES ({placeholders(SCENARIO_COLS)})",
                   rows)


def run(forecasts: Sequence[Forecast], ctx: Any) -> list[Scenario]:
    """Ranked scenarios per forecast (rank 1 = best; rank None = rejected as correlated). Writes FF_AG_SCENARIO."""
    c = resolve_ctx(ctx, "a4")
    as_of = c.run.as_of or datetime.now(timezone.utc).date()
    graph = a2.load_graph(c.db)
    out: list[Scenario] = []
    for fc in forecasts:
        s = load_situation(c.db, fc, as_of, c.rules)
        if s is not None and s.shortfall > 0:
            out.extend(scenarios_for(s, c.run.run_id, c, graph))
    if out and not c.run.dry_run:
        _write(c.db, out)
    return out
