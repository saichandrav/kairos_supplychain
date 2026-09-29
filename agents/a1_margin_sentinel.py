"""A1 Margin Sentinel: is making this formulation still viable under its ceiling price? (plan §3 A1, §4.1)

    realisation  = CP / (1 + retailer_margin) / (1 + wholesaler_margin)
    unit_cost    = api_cost_per_kg × api_g_per_unit / 1000 / yield + conversion + packaging + freight
    headroom_pct = (realisation − unit_cost) / realisation

API cost is the 3-month moving median of the monthly import unit value; conversion and
packaging are indexed to WPI when FF_REF_WPI is present. Trend is the OLS slope of monthly
headroom over the last 6–12 months. All constants come from rules/*.yaml.

Output is a probabilistic flag for review, never a statement about a manufacturer.
The orchestrator audits the returned objects; A1 writes FF_AG_SIGNAL only.
"""
from __future__ import annotations

import argparse
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

import numpy as np
import pandas as pd

from agents.common import AgentCtx, load_fixture_db, placeholders, resolve_ctx, to_tag, try_query, worst_tag
from agents.contracts import DataQuality, DataTag, RiskSignal, RunContext, SecondarySignal, SignalBand
from agents.ctx import Db
from agents.rules import Rules, load_rules

AGENT = "A1"
WPI_SERIES = "MANUFACTURED_PRODUCTS"
# Inputs that count towards data_quality (share tagged REAL).
DQ_INPUTS = ("ceiling_price", "bom", "api_cost", "wpi")

SIGNAL_COLS = (  # S01 db/schema.sql
    "RUN_ID", "FORM_ID", "BAND", "HEADROOM_PCT", "HEADROOM_TREND", "MONTHS_TO_BREACH", "REALISATION_INR",
    "UNIT_COST_INR", "SECONDARY_SIGNALS_JSON", "CONFIDENCE", "DATA_TAGS_JSON", "CREATED_AT",
)


# ---------------------------------------------------------------- helpers

_try_query = try_query
_in = placeholders


def _month(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series.astype(str)).dt.to_period("M")


def _money(x: float | None) -> Decimal | None:
    return None if x is None or not math.isfinite(x) else Decimal(f"{x:.4f}")


_Ctx = AgentCtx


def _resolve_ctx(ctx: Any) -> AgentCtx:
    return resolve_ctx(ctx, "a1")


# ---------------------------------------------------------------- core maths (§4.1)


def realisation(ceiling_price: float, rules: Rules) -> float:
    rm = rules.value("weights", "margin.retailer_margin")
    wm = rules.value("weights", "margin.wholesaler_margin")
    return ceiling_price / (1 + rm) / (1 + wm)


def unit_cost(api_cost_per_kg: float, api_g_per_unit: float, yld: float,
              conversion: float, packaging: float, freight: float) -> float:
    return api_cost_per_kg * api_g_per_unit / 1000 / yld + conversion + packaging + freight


def headroom(real: float, cost: float) -> float:
    return (real - cost) / real


def ols_slope(y: Sequence[float]) -> float:
    """OLS slope per step (month) of y against 0..n-1."""
    x = np.arange(len(y), dtype=float)
    return float(np.polyfit(x, np.asarray(y, dtype=float), 1)[0])


def months_to_breach(h: float, slope: float) -> float | None:
    if slope >= 0 or not math.isfinite(h):
        return None
    return max(h, 0.0) / -slope


def band_for(h: float, mtb: float | None, rules: Rules) -> SignalBand:
    v = lambda k: rules.value("weights", f"bands.{k}")  # noqa: E731
    if not math.isfinite(h):
        return SignalBand.AMBER  # unknown is not safe: flag for review with low confidence
    if h < v("red_headroom_pct") or (mtb is not None and mtb <= v("red_months_to_breach")):
        return SignalBand.RED
    if h < v("amber_headroom_pct") or (mtb is not None and mtb <= v("amber_months_to_breach")):
        return SignalBand.AMBER
    return SignalBand.GREEN


# ---------------------------------------------------------------- inputs


@dataclass
class _Inputs:
    ceiling: pd.DataFrame
    form_api: pd.DataFrame
    bom: pd.DataFrame
    api_cost: pd.DataFrame
    wpi: pd.DataFrame | None
    tags: dict[str, dict[str, DataTag]] = field(default_factory=dict)


def _load_inputs(db: Db, form_ids: list[str]) -> _Inputs:
    q = _in(form_ids)
    ceiling = db.query(
        f"SELECT FORM_ID, EFFECTIVE_FROM, CEILING_PRICE, IS_PROXY FROM FF_REF_CEILING_PRICE WHERE FORM_ID IN ({q})",
        form_ids)
    form_api = db.query(f"SELECT FORM_ID, API_ID, API_MG_PER_UNIT FROM FF_REF_FORM_API WHERE FORM_ID IN ({q})", form_ids)
    bom = db.query(
        "SELECT FORM_ID, YIELD, CONVERSION_COST_INR, PACKAGING_COST_INR, FREIGHT_COST_INR, IS_PROXY "
        f"FROM FF_REF_BOM_ASSUMPTION WHERE FORM_ID IN ({q})", form_ids)
    api_ids = sorted(set(form_api["API_ID"].astype(str)))
    api_cost = (
        db.query(f"SELECT API_ID, MONTH, UNIT_VALUE_INR_KG, IS_PROXY FROM FF_REF_API_COST_MONTHLY "
                 f"WHERE API_ID IN ({_in(api_ids)})", api_ids)
        if api_ids else pd.DataFrame(columns=["API_ID", "MONTH", "UNIT_VALUE_INR_KG", "IS_PROXY"])
    )
    wpi = _try_query(db, "SELECT MONTH, INDEX_VALUE, IS_PROXY FROM FF_REF_WPI WHERE SERIES = ?", (WPI_SERIES,))
    for df in (ceiling, api_cost):
        col = "EFFECTIVE_FROM" if "EFFECTIVE_FROM" in df else "MONTH"
        df["_M"] = _month(df[col]) if len(df) else pd.Series(dtype="period[M]")
    if wpi is not None and len(wpi):
        wpi["_M"] = _month(wpi["MONTH"])
    return _Inputs(ceiling, form_api, bom, api_cost, wpi if wpi is not None and len(wpi) else None)


def _shock_multiplier(api_id: str, api_cost_multiplier: float, run: RunContext) -> float:
    if api_cost_multiplier != 1.0 or run.shock is None:
        return api_cost_multiplier
    if run.shock.api_ids and api_id not in run.shock.api_ids:
        return 1.0
    return 1 + run.shock.api_cost_pct / 100


# ---------------------------------------------------------------- secondary signals


def _secondary(db: Db, form_id: str, as_of: pd.Period, rules: Rules) -> list[SecondarySignal]:
    out: list[SecondarySignal] = []

    lookback = int(rules.param("QUALITY_HISTORY", "nsq_lookback_months"))
    nsq = _try_query(
        db,
        "SELECT n.ALERT_ID, n.MONTH, n.IS_PROXY FROM FF_REF_NSQ_ALERT n WHERE n.FORM_ID = ? "
        "UNION SELECT n.ALERT_ID, n.MONTH, n.IS_PROXY FROM FF_REF_NSQ_ALERT n "
        "JOIN FF_REF_PRODUCER p ON p.MANUFACTURER_ID = n.MANUFACTURER_ID "
        "JOIN FF_REF_FORMULATION f ON f.FORM_ID = p.FORM_ID "
        "WHERE n.FORM_ID IS NULL AND p.FORM_ID = ? AND UPPER(n.DRUG) LIKE '%' || UPPER(f.GENERIC) || '%'",
        (form_id, form_id))
    if nsq is not None and len(nsq):
        m = _month(nsq["MONTH"])
        recent = nsq[(m > as_of - lookback) & (m <= as_of)]
        if len(recent):
            out.append(SecondarySignal(
                kind="NSQ", value=float(len(recent)), tag=worst_tag(recent["IS_PROXY"].tolist()),
                note=f"CDSCO NSQ (FF_REF_NSQ_ALERT): {len(recent)} alert(s) for {form_id} or its producers "
                     f"in the last {lookback} months"))

    # Monthly OTD with the FF_V_SUPPLIER_OTD logic (GR 101 date <= EINDT), which the view only gives as a
    # lifetime figure per vendor. Month = the PO line's delivery date (EINDT).
    w = int(rules.value("weights", "secondary.otd_window_months"))
    otd = _try_query(
        db,
        "SELECT p.EINDT, g.GR_DATE, p.IS_PROXY FROM FF_MM_EKPO p "
        "JOIN FF_MM_MARA a ON a.MATNR = p.MATNR "
        "JOIN (SELECT EBELN, EBELP, MIN(BUDAT) AS GR_DATE FROM FF_MM_MSEG "
        "      WHERE BWART = '101' AND EBELN IS NOT NULL GROUP BY EBELN, EBELP) g "
        "  ON g.EBELN = p.EBELN AND g.EBELP = p.EBELP "
        "WHERE a.FORM_ID = ?", (form_id,))
    if otd is not None and len(otd):
        gr = pd.to_datetime(otd["GR_DATE"].astype(str))
        due = pd.to_datetime(otd["EINDT"].astype(str))
        lines = otd.assign(_M=due.dt.to_period("M"), ON_TIME=(gr <= due).astype(float) * 100)
        lines = lines[(gr.dt.to_period("M") <= as_of) & (lines["_M"] <= as_of)]
        monthly = lines.groupby("_M")["ON_TIME"].mean().sort_index()
        if len(monthly) >= 2 * w:
            drop = float(monthly.iloc[-2 * w:-w].mean() - monthly.iloc[-w:].mean())
            if drop > 0:
                out.append(SecondarySignal(
                    kind="OTD", value=round(drop, 4), tag=worst_tag(lines["IS_PROXY"].tolist()),
                    note=f"FF_MM_EKPO/MSEG (FF_V_SUPPLIER_OTD logic): on-time delivery down {drop:.1f} pts "
                         f"(last {w} months vs previous {w})"))

    days = int(rules.value("weights", "secondary.cold_chain_window_days"))
    cc = _try_query(
        db,
        "SELECT c.TS, c.LOCATION FROM FF_MM_COLDCHAIN c "
        "WHERE c.EXCURSION_FLAG IN ('Y', 'X', '1', 1) AND c.LOCATION IN ("
        "  SELECT b.LGORT FROM FF_MM_MCHB b JOIN FF_MM_MARA a ON a.MATNR = b.MATNR WHERE a.FORM_ID = ?)",
        (form_id,))
    if cc is not None and len(cc):
        end = as_of.to_timestamp(how="end")
        ts = pd.to_datetime(cc["TS"].astype(str))
        n = int(((ts > end - pd.Timedelta(days=days)) & (ts <= end)).sum())
        if n:
            out.append(SecondarySignal(
                kind="COLD_CHAIN", value=float(n), tag=DataTag.SYNTH,
                note=f"FF_MM_COLDCHAIN: {n} excursion(s) in the last {days} days at locations holding {form_id}"))
    return out


# ---------------------------------------------------------------- run


def _evaluate(form_id: str, inp: _Inputs, c: _Ctx, api_cost_multiplier: float
              ) -> tuple[RiskSignal, dict[str, Any]]:
    rules = c.rules
    tags: dict[str, DataTag] = {}
    extra: dict[str, Any] = {"as_of": None, "realisation": None, "unit_cost": None}

    ceil = inp.ceiling[inp.ceiling["FORM_ID"] == form_id].sort_values("_M")
    fa = inp.form_api[inp.form_api["FORM_ID"] == form_id]
    bom = inp.bom[inp.bom["FORM_ID"] == form_id]
    if len(ceil):
        tags["ceiling_price"] = worst_tag(ceil["IS_PROXY"])
    if len(bom) and len(fa):
        tags["bom"] = to_tag(bom.iloc[0]["IS_PROXY"])
    if inp.wpi is not None:
        tags["wpi"] = worst_tag(inp.wpi["IS_PROXY"])

    series: list[tuple[pd.Period, float, float, float]] = []  # (month, realisation, unit_cost, headroom)
    if len(ceil) and len(bom) and len(fa):
        api_id = str(fa.iloc[0]["API_ID"])
        g_per_unit = float(fa.iloc[0]["API_MG_PER_UNIT"]) / 1000
        b = bom.iloc[0]
        cost = inp.api_cost[inp.api_cost["API_ID"].astype(str) == api_id].sort_values("_M")
        if c.run.as_of is not None:
            cost = cost[cost["_M"] <= pd.Period(c.run.as_of, "M")]
        if len(cost):
            tags["api_cost"] = worst_tag(cost["IS_PROXY"])
            mult = _shock_multiplier(api_id, api_cost_multiplier, c.run)
            n_med = int(rules.value("weights", "margin.api_cost_median_months"))
            med = (cost["UNIT_VALUE_INR_KG"].astype(float) * mult).rolling(n_med, min_periods=1).median()
            wpi = inp.wpi.groupby("_M")["INDEX_VALUE"].mean().astype(float) if inp.wpi is not None else None
            base_wpi = float(wpi.iloc[-1]) if wpi is not None else None
            for m, api_kg in zip(cost["_M"], med):
                cp_rows = ceil[ceil["_M"] <= m]
                if not len(cp_rows):
                    continue
                idx = float(wpi[wpi.index <= m].iloc[-1]) / base_wpi if wpi is not None and (wpi.index <= m).any() else 1.0
                r = realisation(float(cp_rows.iloc[-1]["CEILING_PRICE"]), rules)
                u = unit_cost(api_kg, g_per_unit, float(b["YIELD"]),
                              float(b["CONVERSION_COST_INR"]) * idx, float(b["PACKAGING_COST_INR"]) * idx,
                              float(b["FREIGHT_COST_INR"]))
                series.append((m, r, u, headroom(r, u)))

    wmin = int(rules.value("weights", "margin.trend_window_min_months"))
    wmax = int(rules.value("weights", "margin.trend_window_max_months"))
    if series:
        as_of, r, u, h = series[-1]
        window = [s[3] for s in series[-wmax:]]
        slope = ols_slope(window) if len(window) >= wmin else 0.0
        extra.update(as_of=str(as_of), realisation=r, unit_cost=u, trend_points=len(window))
    else:
        r = u = None
        h, slope = float("nan"), 0.0
    mtb = months_to_breach(h, slope)
    real_share = sum(1 for k in DQ_INPUTS if tags.get(k) is DataTag.REAL) / len(DQ_INPUTS)

    as_of_p = pd.Period(extra["as_of"], "M") if extra["as_of"] else (
        pd.Period(c.run.as_of, "M") if c.run.as_of else pd.Period(datetime.now(timezone.utc).date(), "M"))
    sig = RiskSignal(
        run_id=c.run.run_id,
        formulation_id=form_id,
        band=band_for(h, mtb, rules),
        headroom_pct=h,
        headroom_trend=slope,
        months_to_breach=mtb,
        realisation_inr=_money(r),
        unit_cost_inr=_money(u),
        secondary_signals=_secondary(c.db, form_id, as_of_p, rules),
        data_quality=DataQuality(confidence=round(real_share, 4), tags=tags),
    )
    return sig, extra


def _write(db: Db, sigs: list[RiskSignal]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    num = (lambda x: None if x is None or (isinstance(x, float) and not math.isfinite(x)) else x)
    money = (lambda d: None if d is None else (float(d) if db.backend == "sqlite" else d))
    rows = [
        (s.run_id, s.formulation_id, s.band.value, num(s.headroom_pct), num(s.headroom_trend),
         num(s.months_to_breach), money(s.realisation_inr), money(s.unit_cost_inr),
         json.dumps([x.model_dump(mode="json") for x in s.secondary_signals]),
         s.data_quality.confidence, json.dumps({k: v.value for k, v in s.data_quality.tags.items()}), now)
        for s in sigs
    ]
    db.executemany(f"INSERT INTO FF_AG_SIGNAL ({', '.join(SIGNAL_COLS)}) VALUES ({_in(SIGNAL_COLS)})", rows)


def run(form_ids: Sequence[str], ctx: Any, api_cost_multiplier: float = 1.0) -> list[RiskSignal]:
    """Compute one RiskSignal per formulation and write them to FF_AG_SIGNAL (unless run.dry_run).

    `ctx` is a Db, or an object with `.db` and optional `.rules` (Rules) and `.run` (RunContext).
    `api_cost_multiplier` scales the whole API cost series (a sustained shock, e.g. 1.3 = +30%).
    """
    if api_cost_multiplier <= 0:
        raise ValueError("api_cost_multiplier must be > 0")
    c = _resolve_ctx(ctx)
    ids = [str(f) for f in (form_ids or c.run.formulation_ids)]
    if not ids:
        ids = c.db.query("SELECT DISTINCT FORM_ID FROM FF_REF_CEILING_PRICE")["FORM_ID"].astype(str).tolist()
    if not ids:
        return []
    inp = _load_inputs(c.db, ids)
    results = [_evaluate(f, inp, c, api_cost_multiplier) for f in ids]
    sigs = [s for s, _ in results]
    if not c.run.dry_run:
        _write(c.db, sigs)
    return sigs


# ---------------------------------------------------------------- CLI


_fixture_db = load_fixture_db


def _has_reference(db: Db) -> bool:
    df = _try_query(db, "SELECT COUNT(*) AS N FROM FF_REF_CEILING_PRICE")
    return df is not None and int(df.iloc[0, 0]) > 0


def format_table(sigs: list[RiskSignal]) -> str:
    hdr = f"{'FORM':<8}{'BAND':<7}{'HEADROOM':>9}{'TREND/MO':>10}{'M2BREACH':>9}{'REALIS.':>9}{'UNITCOST':>9}{'CONF':>6}  SECONDARY"
    lines = [hdr, "-" * len(hdr)]
    for s in sigs:
        h = f"{s.headroom_pct:.2%}" if math.isfinite(s.headroom_pct) else "n/a"
        mtb = f"{s.months_to_breach:.1f}" if s.months_to_breach is not None else "-"
        sec = ",".join(f"{x.kind}={x.value:g}" for x in s.secondary_signals) or "-"
        lines.append(
            f"{s.formulation_id:<8}{s.band.value:<7}{h:>9}{s.headroom_trend:>+10.4f}{mtb:>9}"
            f"{str(s.realisation_inr or '-'):>9}{str(s.unit_cost_inr or '-'):>9}{s.data_quality.confidence:>6.2f}  {sec}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    from agents.ctx import get_db

    p = argparse.ArgumentParser(prog="python -m agents.a1_margin_sentinel", description=__doc__.split("\n")[0])
    p.add_argument("--form", action="append", default=[], help="formulation id (repeatable; default: all)")
    p.add_argument("--shock", type=float, default=1.0, help="API cost multiplier, e.g. 1.3 = +30%%")
    p.add_argument("--fixtures", action="store_true", help="use the SYNTH test fixtures in memory")
    p.add_argument("--dry-run", action="store_true", help="don't write FF_AG_SIGNAL")
    a = p.parse_args(argv)

    db = _fixture_db() if a.fixtures else get_db()
    if not a.fixtures and db.backend == "sqlite" and not _has_reference(db):
        print("[a1] no reference data in data/kairos.sqlite; using SYNTH test fixtures in memory")
        db.close()
        db = _fixture_db()
    run_ctx = RunContext(run_id=f"a1-cli-{uuid.uuid4().hex[:8]}", dry_run=a.dry_run)
    with db:
        sigs = run(a.form, _Ctx(db, load_rules(), run_ctx), api_cost_multiplier=a.shock)
    print(f"run_id={run_ctx.run_id} shock=x{a.shock:g}  (probabilistic flags for review)")
    print(format_table(sigs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
