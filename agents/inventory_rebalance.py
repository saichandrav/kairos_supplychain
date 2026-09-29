"""Inventory Rebalancing Agent: safety stock, storage-location transfers and procurement triggers under a disruption.

For every formulation the Scenario Planning Agent found exposed, it:
  1. recomputes safety stock and reorder point for the disrupted lead time:
        SS  = z(VED) x sqrt((L + delay) x sd_daily^2 + daily^2 x (k x delay)^2)
        ROP = daily x (L + delay) + SS
     with the delay taken from the Logistics re-plan when the material has inbound shipments;
  2. moves stock between storage locations (S/4 movement 311): issuing locations below the minimum cover are
     topped up to the target cover, never taking a donor below max(floor, target) days of its own demand;
  3. turns any remaining net requirement (ROP - usable - open PO qty) into LOGISTICS_DELAY forecasts that the
     orchestrator feeds to the existing A4 optimiser and A5 validator, so every purchase still goes through the
     Chief Pharmacist gate and A6. `screen` rejects A4 options that don't actually escape the disruption.
All constants: rules/disruption.yaml (inventory.*) and rules/weights.yaml (exposure.lead_time_days).
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Sequence

import pandas as pd

from agents.common import as_of_date, db_ts, resolve_ctx, try_query, upsert_many
from agents.contracts import (
    CauseCode, Forecast, InventoryPlan, LogisticsPlan, OptionType, SafetyStockUpdate, Scenario, ScenarioPlan,
    SignalBand, TransferOrder,
)
from agents.ctx import Db
from agents.rules import Rules

if TYPE_CHECKING:
    from agents.scenario_planning import DisruptionState

AGENT = "DX4_INVENTORY"
DAYS_PER_MONTH = 30.44
ISSUE, REVERSAL = ("261", "201"), ("262", "202")


def _r(rules: Rules, path: str) -> Any:
    return rules.value("disruption", path)


def _z(ved: str | None, rules: Rules) -> float:
    try:
        return float(_r(rules, f"inventory.service_z.{(ved or 'E').strip().upper()}"))
    except KeyError:
        return float(_r(rules, "inventory.service_z.E"))


def _daily_stats(db: Db, materials: Sequence[str], as_of, window: int) -> dict[str, tuple[float, float]]:
    """material -> (mean, sd) of net daily issues over the window, zero-issue days included."""
    if not materials:
        return {}
    mats = sorted(set(materials))
    start = (as_of - timedelta(days=window)).isoformat()
    df = try_query(db, "SELECT MATNR, BUDAT, BWART, MENGE FROM FF_MM_MSEG WHERE BWART IN ('261', '201', '262', '202') "
                       f"AND BUDAT > ? AND BUDAT <= ? AND MATNR IN ({', '.join('?' for _ in mats)})",
                   [start, as_of.isoformat(), *mats])
    out = {m: (0.0, 0.0) for m in mats}
    if df is None or df.empty:
        return out
    df["Q"] = pd.to_numeric(df["MENGE"], errors="coerce").fillna(0.0)
    df.loc[df["BWART"].astype(str).isin(REVERSAL), "Q"] *= -1
    df["DAY"] = pd.to_datetime(df["BUDAT"].astype(str)).dt.date
    for m, g in df.groupby(df["MATNR"].astype(str)):
        daily = g.groupby("DAY")["Q"].sum()
        full = [float(daily.get(as_of - timedelta(days=i), 0.0)) for i in range(window)]
        mean = sum(full) / window
        sd = math.sqrt(sum((x - mean) ** 2 for x in full) / max(1, window - 1))
        out[m] = (mean, sd)
    return out


def _location_stock(db: Db, materials: Sequence[str]) -> pd.DataFrame:
    """Usable (FEFO) stock and issue share per material x plant x storage location."""
    mats = sorted(set(materials))
    cols = ["MATNR", "WERKS", "LGORT", "USABLE", "ISSUE_SHARE"]
    if not mats:
        return pd.DataFrame(columns=cols)
    marks = ", ".join("?" for _ in mats)
    stock = try_query(db, f"SELECT MATNR, WERKS, LGORT, SUM(USABLE_QTY) AS USABLE FROM FF_V_BATCH_FEFO "
                          f"WHERE MATNR IN ({marks}) GROUP BY MATNR, WERKS, LGORT", mats)
    issues = try_query(db, f"SELECT MATNR, WERKS, LGORT, SUM(MENGE) AS Q FROM FF_MM_MSEG WHERE BWART IN ('261', '201') "
                           f"AND MATNR IN ({marks}) GROUP BY MATNR, WERKS, LGORT", mats)
    stock = stock if stock is not None else pd.DataFrame(columns=["MATNR", "WERKS", "LGORT", "USABLE"])
    issues = issues if issues is not None else pd.DataFrame(columns=["MATNR", "WERKS", "LGORT", "Q"])
    df = stock.merge(issues, on=["MATNR", "WERKS", "LGORT"], how="outer")
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["USABLE"] = pd.to_numeric(df["USABLE"], errors="coerce").fillna(0.0)
    df["Q"] = pd.to_numeric(df["Q"], errors="coerce").fillna(0.0)
    tot = df.groupby(["MATNR", "WERKS"])["Q"].transform("sum")
    df["ISSUE_SHARE"] = (df["Q"] / tot.where(tot > 0)).fillna(0.0)
    return df[cols].dropna(subset=["LGORT"])


def plan_transfers(run_id: str, material: str, daily: float, locs: pd.DataFrame, cold: bool,
                   rules: Rules) -> list[TransferOrder]:
    """Greedy surplus -> deficit moves: top up issuing locations near stock-out, never draining a donor."""
    if daily <= 0 or len(locs) < 2:
        return []
    min_days = float(_r(rules, "inventory.location_min_cover_days"))
    target_days = float(_r(rules, "inventory.location_target_cover_days"))
    floor_days = float(_r(rules, "inventory.donor_floor_cover_days"))
    rows = [{"werks": str(r.WERKS), "lgort": str(r.LGORT), "usable": float(r.USABLE),
             "daily": daily * float(r.ISSUE_SHARE)} for r in locs.itertuples()]
    cover = lambda x: x["usable"] / x["daily"] if x["daily"] > 0 else math.inf  # noqa: E731
    need = sorted((x for x in rows if x["daily"] > 0 and cover(x) < min_days), key=cover)
    out: list[TransferOrder] = []
    for rcv in need:
        want = math.ceil(target_days * rcv["daily"] - rcv["usable"])
        before = cover(rcv)
        donors = sorted((x for x in rows if x is not rcv and x["werks"] == rcv["werks"]),
                        key=lambda x: -(x["usable"] - max(floor_days, target_days) * x["daily"]))
        for d in donors:
            spare = math.floor(d["usable"] - max(floor_days, target_days) * d["daily"])
            qty = min(want, spare)
            if qty < max(1, math.ceil(rcv["daily"])):
                continue
            d["usable"] -= qty
            rcv["usable"] += qty
            want -= qty
            out.append(TransferOrder(
                transfer_id=f"{run_id}:T{len(out) + 1:02d}:{material}"[:60], material=material, plant=rcv["werks"],
                from_location=d["lgort"], to_location=rcv["lgort"], qty=qty, cover_to_before=round(before, 1),
                cover_to_after=round(cover(rcv), 1), cover_from_after=round(min(cover(d), 9999.0), 1), cold_chain=cold))
            if want < max(1, math.ceil(rcv["daily"])):
                break
    return out


def plan(sp_plan: ScenarioPlan, lp: LogisticsPlan | None, ctx: Any) -> InventoryPlan:
    """Safety stock, transfers and procurement forecasts for every exposed formulation."""
    c = resolve_ctx(ctx, "iv")
    db, rules, run_id = c.db, c.rules, c.run.run_id
    as_of = c.run.as_of or as_of_date(db)
    lead = float(rules.value("weights", "exposure.lead_time_days"))
    k_sd = float(_r(rules, "inventory.delay_sd_factor"))
    window = int(_r(rules, "inventory.demand_window_days"))
    mats = [i.material for i in sp_plan.impacts]
    stats = _daily_stats(db, mats, as_of, window)
    locs = _location_stock(db, mats)
    mara = try_query(db, f"SELECT MATNR, RAUBE FROM FF_MM_MARA WHERE MATNR IN ({', '.join('?' for _ in mats)})", mats) \
        if mats else None
    cold = {str(r.MATNR): str(r.RAUBE or "").upper().startswith("COLD") for r in mara.itertuples()} \
        if mara is not None else {}
    ship_delay: dict[str, int] = {}
    for s in (lp.shipments if lp else []):
        ship_delay[s.material] = max(ship_delay.get(s.material, 0), s.chosen.delay_days)

    ss_rows, transfers, forecasts = [], [], []
    for imp in sp_plan.impacts:
        d = imp.daily_consumption
        mean, sd = stats.get(imp.material, (d, 0.0))
        if imp.material in ship_delay:
            delay = float(ship_delay[imp.material])
        elif imp.recommended and imp.recommended.value == "REROUTE":
            delay = imp.exposure_days_reroute
        else:
            delay = imp.exposure_days_absorb
        z = _z(imp.ved, rules)
        ss_old = z * sd * math.sqrt(lead)
        ss_new = z * math.sqrt((lead + delay) * sd ** 2 + d ** 2 * (k_sd * delay) ** 2)
        rop = d * (lead + delay) + ss_new
        mine = locs[locs["MATNR"].astype(str) == imp.material]
        plant = str(mine.sort_values("USABLE").iloc[-1]["WERKS"]) if len(mine) else "H001"
        net = max(0.0, math.ceil(rop - imp.usable_stock - imp.open_po_qty))
        ss_rows.append(SafetyStockUpdate(
            material=imp.material, plant=plant, formulation_id=imp.formulation_id, daily_mean=round(mean, 4),
            daily_sd=round(sd, 4), lead_time_days=lead, delay_days=round(delay, 2), service_z=z,
            safety_stock_old=round(ss_old, 1), safety_stock_new=round(ss_new, 1), reorder_point_new=round(rop, 1),
            usable_stock=imp.usable_stock, open_po_qty=imp.open_po_qty, net_requirement=net))
        transfers += plan_transfers(run_id, imp.material, d, mine, cold.get(imp.material, False), rules)
        if net > 0 and d > 0:
            horizon_days = rop / d
            short_days = max(0.0, (imp.days_of_cover or 0.0) - lead - delay)
            forecasts.append(Forecast(
                run_id=run_id, formulation_id=imp.formulation_id,
                exit_risk_band=SignalBand.RED if short_days <= 0 and str(imp.ved).upper() == "V" else SignalBand.AMBER,
                days_of_cover=imp.days_of_cover, cause_code=CauseCode.LOGISTICS_DELAY,
                window_months_lo=round((lead + delay) / DAYS_PER_MONTH, 4),
                window_months_hi=round(horizon_days / DAYS_PER_MONTH, 4),
                cause_facts={
                    "hospital_materials": {"value": [imp.material], "tag": "SYNTH"},
                    "daily_consumption": {"value": round(d, 4), "tag": "SYNTH"},
                    "usable_stock_qty": {"value": round(imp.usable_stock + imp.open_po_qty, 4), "tag": "SYNTH"},
                    "delay_days": {"value": round(delay, 2), "tag": "PROXY"},
                    "reorder_point": {"value": round(rop, 1), "tag": "PROXY"},
                    "net_requirement": {"value": net, "tag": "PROXY"},
                }))
    return InventoryPlan(run_id=run_id, safety_stock=ss_rows, transfers=transfers, procurement=forecasts)


def screen(scenarios: Sequence[Scenario], sp_plan: ScenarioPlan, state: "DisruptionState",
           shares: dict[str, dict[str, dict[str, float]]], rules: Rules) -> list[Scenario]:
    """Reject A4 options that don't escape the disruption, then re-rank the rest per formulation.

    REBALANCE is dropped (plan_transfers already balances every location). An alternate manufacturer of the same
    formulation imports the same API, so it is rejected when the exposure comes through origin lanes. A therapeutic
    substitute is rejected when its own API depends on the disrupted origins beyond the rules threshold.
    """
    impacts = {i.formulation_id: i for i in sp_plan.impacts}
    bad_origins = {l.origin for i in sp_plan.impacts for l in i.lanes
                   if l.absorb_delay_days > 0 or l.supply_loss > 0 or l.security_risk}
    max_exposed = float(_r(rules, "inventory.substitute_max_exposed_share"))
    out: list[Scenario] = []
    for s in scenarios:
        if s.option_type is OptionType.REBALANCE:
            continue
        reason = None
        imp = impacts.get(s.formulation_id)
        lane_hit = [l for l in (imp.lanes if imp else []) if l.origin in bad_origins]
        if s.supplier and s.supplier in state.offline_suppliers:
            reason = f"supplier {s.supplier} is offline in this disruption"
        elif s.option_type is OptionType.ALT_SUPPLIER and lane_hit:
            top = max(lane_hit, key=lambda l: l.share)
            reason = (f"alternate manufacturer imports the same API; {top.share:.0%} comes from disrupted origin "
                      f"{top.origin}")
        elif s.option_type is OptionType.THERAPEUTIC_ALT and s.substitute_formulation_id:
            exposed = max((sum(v for o, v in by.items() if o in bad_origins)
                           for by in shares.get(s.substitute_formulation_id, {}).values()), default=0.0)
            if exposed >= max_exposed:
                reason = f"substitute's API is {exposed:.0%} sourced from the disrupted origins"
        if reason:
            out.append(s.model_copy(update={"rank": None, "correlated_risk_flag": True, "correlated_risk_reason": reason}))
        else:
            out.append(s)
    ranked: dict[str, int] = {}
    final = []
    for s in sorted(out, key=lambda s: (s.formulation_id, s.rank is None, s.rank or 0, s.scenario_id)):
        if s.rank is not None:
            ranked[s.formulation_id] = ranked.get(s.formulation_id, 0) + 1
            s = s.model_copy(update={"rank": ranked[s.formulation_id]})
        final.append(s)
    return final


# ---------------------------------------------------------------- persist / apply


def save(ip: InventoryPlan, db: Db) -> None:
    now = db_ts()
    upsert_many(db, "FF_AG_SAFETY_STOCK", ("RUN_ID", "MATNR", "WERKS"), [{
        "RUN_ID": ip.run_id, "MATNR": s.material, "WERKS": s.plant, "DAILY_MEAN": s.daily_mean,
        "DAILY_SD": s.daily_sd, "LEAD_TIME_DAYS": s.lead_time_days, "DELAY_DAYS": s.delay_days,
        "SERVICE_Z": s.service_z, "SAFETY_STOCK_OLD": s.safety_stock_old, "SAFETY_STOCK_NEW": s.safety_stock_new,
        "REORDER_POINT_NEW": s.reorder_point_new, "NET_REQUIREMENT": s.net_requirement, "CREATED_AT": now}
        for s in ip.safety_stock])
    upsert_many(db, "FF_AG_TRANSFER", ("TRANSFER_ID",), [{
        "TRANSFER_ID": t.transfer_id, "RUN_ID": ip.run_id, "MATNR": t.material, "WERKS": t.plant,
        "FROM_LGORT": t.from_location, "TO_LGORT": t.to_location, "QTY": t.qty, "COLD_CHAIN": int(t.cold_chain),
        "STATUS": t.status, "DECISION_ID": t.decision_id, "CREATED_AT": now} for t in ip.transfers])


def apply_decision(decision_id: str, approved: bool, db: Db) -> dict[str, Any]:
    status = "EXECUTED" if approved else "REJECTED"
    n = db.execute("UPDATE FF_AG_TRANSFER SET STATUS = ? WHERE DECISION_ID = ?", (status, decision_id))
    if not n:
        raise ValueError(f"no transfer for decision {decision_id}")
    return {"decision_id": decision_id, "status": status}


def transfers(db: Db, run_id: str) -> list[dict[str, Any]]:
    df = try_query(db, "SELECT TRANSFER_ID, MATNR, WERKS, FROM_LGORT, TO_LGORT, QTY, COLD_CHAIN, STATUS, DECISION_ID "
                       "FROM FF_AG_TRANSFER WHERE RUN_ID = ? ORDER BY TRANSFER_ID", (run_id,))
    return [] if df is None or df.empty else df.where(df.notna(), None).to_dict(orient="records")


def safety_stock(db: Db, run_id: str) -> list[dict[str, Any]]:
    df = try_query(db, "SELECT MATNR, WERKS, DAILY_MEAN, DAILY_SD, LEAD_TIME_DAYS, DELAY_DAYS, SERVICE_Z, "
                       "SAFETY_STOCK_OLD, SAFETY_STOCK_NEW, REORDER_POINT_NEW, NET_REQUIREMENT "
                       "FROM FF_AG_SAFETY_STOCK WHERE RUN_ID = ? ORDER BY NET_REQUIREMENT DESC, MATNR", (run_id,))
    return [] if df is None or df.empty else df.where(df.notna(), None).to_dict(orient="records")
