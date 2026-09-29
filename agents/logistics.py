"""Rerouting & Logistics Agent: re-plans inbound supply on disrupted lanes (plan: disruption response).

For every open PO line (EKPO with no goods receipt) whose formulation depends on a disrupted API lane, it:
  1. lists every feasible way to move the consignment: each mapped sea lane as-is, rerouted around the bad
     chokepoints, held until reopening, or air freight (rules/sea_routes.yaml, rules/disruption.yaml);
  2. prices each option (surcharge, war-risk, air freight) and the units that would arrive after the
     projected stock-out date, and allocates consignments across the carrier panel within capacity,
     most urgent first (lowest cover, VED V before E before D);
  3. revises the delivery date and drafts notices to the supplier, forwarder, affected wards and procurement.
It never decides autonomy itself: the Compliance agent classifies each action AUTO / HUMAN / BLOCKED, and
notices stay HELD until a human approves a gated routing.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pandas as pd

from agents import sea_routes
from agents.common import as_of_date, db_ts, lob_cols, lob_value, po_lines, resolve_ctx, try_query, upsert_many
from agents.contracts import LogisticsPlan, Notification, RouteChoice, ScenarioPlan, ShipmentAction
from agents.ctx import Db
from agents.rules import Rules

if TYPE_CHECKING:
    from agents.scenario_planning import DisruptionState

AGENT = "DX3_LOGISTICS"
VED_ORDER = {"V": 0, "E": 1, "D": 2}
ISSUES = ("261", "201")


def _r(rules: Rules, path: str) -> Any:
    return rules.value("disruption", path)


def _money(x: float) -> Decimal:
    return Decimal(f"{x:.4f}")


def _options(origin: str, share: float, value: float, unit_value: float, daily: float, base_eta: date,
             stockout: date | None, as_of: date, state: "DisruptionState", rules: Rules) -> list[tuple[float, RouteChoice]]:
    """(objective, choice) for every feasible mode x lane x carrier, cheapest objective first."""
    k = sea_routes.consts_from_rules(rules)
    passthrough = float(_r(rules, "scenario.lead_time_passthrough"))
    per_day = float(_r(rules, "logistics.reroute_surcharge_pct_per_day"))
    war = float(_r(rules, "logistics.war_risk_surcharge_pct"))
    loss_p = float(_r(rules, "logistics.security_loss_probability"))
    penalty = float(_r(rules, "logistics.late_penalty_multiplier"))
    carriers = _r(rules, "logistics.carriers")

    def late(eta: date) -> int:
        return int(math.ceil(daily * max(0, (eta - stockout).days))) if stockout and daily > 0 else 0

    out: list[tuple[float, RouteChoice]] = []
    for o in sea_routes.lane_options(origin, state.lanes, k):
        if o.blocked:
            continue
        delay = int(math.ceil(passthrough * share * max(0, o.extra_days)))
        eta = base_eta + timedelta(days=delay)
        surcharge = value * share * (per_day * max(0, o.extra_days) + (war if o.security_risk else 0.0))
        expected_loss = value * share * loss_p if o.security_risk else 0.0
        for car in carriers:
            if car["mode"] != "SEA" or (o.security_risk and car["avoids_security_risk"]):
                continue
            cost = surcharge * float(car["cost_factor"])
            ch = RouteChoice(option_id=f"{o.option_id}:{car['id']}", label=o.label, mode="SEA", carrier=car["id"],
                             via=list(o.via), new_eta=eta, delay_days=delay, extra_cost_inr=_money(cost),
                             late_units=late(eta), security_risk=o.security_risk)
            out.append((cost + expected_loss + ch.late_units * unit_value * penalty + (1 - float(car["reliability"])), ch))
    if value <= float(_r(rules, "logistics.air_max_consignment_value_inr")):
        eta = max(base_eta, as_of + timedelta(days=int(_r(rules, "scenario.air_transit_days"))))
        for car in (c for c in carriers if c["mode"] == "AIR"):
            cost = value * share * float(_r(rules, "logistics.air_freight_pct_of_value")) * float(car["cost_factor"])
            ch = RouteChoice(option_id=f"{origin}-AIR:{car['id']}", label="Air freight (expedite)", mode="AIR",
                             carrier=car["id"], new_eta=eta, delay_days=(eta - base_eta).days,
                             extra_cost_inr=_money(cost), late_units=late(eta))
            out.append((cost + ch.late_units * unit_value * penalty + (1 - float(car["reliability"])), ch))
    out.sort(key=lambda t: (t[0], t[1].option_id))
    return out


def _dominant_lane(impact) -> Any:
    lanes = [l for l in impact.lanes if l.absorb_delay_days > 0 or l.security_risk or l.reroute_delay_days is not None]
    return max(lanes, key=lambda l: (l.share, l.origin)) if lanes else None


def plan(state: "DisruptionState", sp_plan: ScenarioPlan, ctx: Any) -> LogisticsPlan:
    """Re-plan every open PO line on a disrupted lane and draft partner notices (statuses set by Compliance)."""
    c = resolve_ctx(ctx, "lg")
    db, rules, run_id = c.db, c.rules, c.run.run_id
    as_of = c.run.as_of or as_of_date(db)
    impacts = {i.material: i for i in sp_plan.impacts}
    open_po = po_lines(db, open_only=True)
    open_po = open_po[open_po["MATNR"].astype(str).isin(impacts)]
    candidates = []
    for line in open_po.itertuples():
        imp = impacts[str(line.MATNR)]
        lane = _dominant_lane(imp)
        if lane is None:
            continue
        unit_value = float(line.UNIT_NET) if pd.notna(line.UNIT_NET) else float(imp.unit_rate_inr or 0)
        value = float(line.MENGE) * unit_value
        stockout = as_of + timedelta(days=int(imp.days_of_cover)) if imp.days_of_cover is not None else None
        base_eta = max(line.EINDT, as_of)
        opts = _options(lane.origin, lane.share, value, unit_value, imp.daily_consumption, base_eta, stockout, as_of,
                        state, rules)
        if opts:
            candidates.append((line, imp, lane, value, stockout, opts))

    candidates.sort(key=lambda t: (t[4] or date.max, VED_ORDER.get(str(t[1].ved or "E").upper(), 1), t[0].EBELN))
    capacity = {car["id"]: int(car["capacity"]) for car in _r(rules, "logistics.carriers")}
    load = {cid: 0 for cid in capacity}
    shipments: list[ShipmentAction] = []
    for line, imp, lane, value, stockout, opts in candidates:
        choices = [ch for _, ch in opts]
        chosen = next((ch for ch in choices if load[ch.carrier] < capacity[ch.carrier]), choices[0])
        load[chosen.carrier] += 1
        seen, alts = {chosen.option_id.split(":")[0]}, []
        for ch in choices:
            key = ch.option_id.split(":")[0]
            if key not in seen:
                seen.add(key)
                alts.append(ch)
        shipments.append(ShipmentAction(
            ebeln=str(line.EBELN), ebelp=int(line.EBELP), material=str(line.MATNR), formulation_id=imp.formulation_id,
            supplier=str(line.LIFNR), origin=lane.origin, qty=float(line.MENGE), value_inr=_money(value),
            old_eta=line.EINDT, stockout_date=stockout, chosen=chosen, alternatives=alts[:3]))

    notices = _notices(run_id, shipments, sp_plan, db)
    return LogisticsPlan(run_id=run_id, shipments=shipments, notifications=notices,
                         total_extra_cost_inr=_money(sum(float(s.chosen.extra_cost_inr) for s in shipments)),
                         carrier_load={k: v for k, v in load.items() if v})


# ---------------------------------------------------------------- notices


def _disruption_label(sp_plan: ScenarioPlan) -> str:
    return "; ".join(f"{s.kind.value.replace('_', ' ').lower()} at {s.target}" for s in sp_plan.specs) or "disruption"


def _notices(run_id: str, shipments: list[ShipmentAction], sp_plan: ScenarioPlan, db: Db) -> list[Notification]:
    if not shipments:
        return []
    what = _disruption_label(sp_plan)
    names = {i.material: (i.name or i.material) for i in sp_plan.impacts}
    vend = try_query(db, "SELECT LIFNR, NAME1 FROM FF_MM_LFA1")
    vendor_name = dict(zip(vend["LIFNR"].astype(str), vend["NAME1"].astype(str))) if vend is not None else {}
    out: list[Notification] = []
    for i, s in enumerate(shipments, 1):
        ch = s.chosen
        body = (f"Due to {what}, the expected delivery of PO {s.ebeln}/{s.ebelp} ({names.get(s.material, s.material)}, "
                f"{s.qty:g} units) moves from {s.old_eta} to {ch.new_eta}. Requested routing for the upstream API "
                f"consignment from {sea_routes.origin_name(s.origin or '')}: {ch.label} ({ch.mode}, carrier {ch.carrier}). "
                f"Hospital cost-share for the change: up to INR {float(ch.extra_cost_inr):.0f}. Please confirm or propose "
                f"an alternative.")
        out.append(Notification(notification_id=f"{run_id}:N{i:03d}S", audience="SUPPLIER",
                                recipient=vendor_name.get(s.supplier or "", s.supplier or "supplier"),
                                subject=f"PO {s.ebeln}/{s.ebelp}: revised delivery {ch.new_eta}", body=body))
    by_carrier: dict[str, list[ShipmentAction]] = {}
    for s in shipments:
        by_carrier.setdefault(s.chosen.carrier or "-", []).append(s)
    for carrier, items in sorted(by_carrier.items()):
        lines = "; ".join(f"PO {s.ebeln}/{s.ebelp} {s.chosen.label} ETA {s.chosen.new_eta}" for s in items)
        out.append(Notification(notification_id=f"{run_id}:F-{carrier}", audience="FORWARDER", recipient=carrier,
                                subject=f"Routing instruction: {len(items)} consignment(s)",
                                body=f"Due to {what}, please book: {lines}."[:4000]))
    late = [s for s in shipments if s.chosen.delay_days > 0]
    for loc, mats in _consuming_locations([s.material for s in late], db).items():
        rows = [s for s in late if s.material in mats]
        lines = "; ".join(f"{names.get(s.material, s.material)}: +{s.chosen.delay_days} days (ETA {s.chosen.new_eta})"
                          for s in rows)
        out.append(Notification(notification_id=f"{run_id}:W-{loc}", audience="WARD", recipient=f"Store {loc}",
                                subject=f"Supply delay notice: {len(rows)} medicine(s)",
                                body=f"Due to {what}, these deliveries are late: {lines}. Please follow the "
                                     f"pharmacy's conservation guidance until stock is confirmed."[:4000]))
    total = sum(float(s.chosen.extra_cost_inr) for s in shipments)
    out.append(Notification(notification_id=f"{run_id}:P", audience="PROCUREMENT", recipient="Pharmacy Procurement",
                            subject=f"Logistics re-plan: {len(shipments)} open PO line(s)",
                            body=f"Due to {what}: {len(shipments)} open PO lines re-planned, {len(late)} delayed, "
                                 f"estimated extra freight INR {total:.0f}. Gated routings wait in the decision queue."))
    return out


def _consuming_locations(materials: list[str], db: Db) -> dict[str, set[str]]:
    """Storage location -> materials it issues (MSEG 261/201), for ward notices."""
    if not materials:
        return {}
    mats = sorted(set(materials))
    df = try_query(db, f"SELECT DISTINCT MATNR, LGORT FROM FF_MM_MSEG WHERE BWART IN ('261', '201') "
                       f"AND MATNR IN ({', '.join('?' for _ in mats)})", mats)
    out: dict[str, set[str]] = {}
    if df is not None:
        for r in df.dropna(subset=["LGORT"]).itertuples():
            out.setdefault(str(r.LGORT), set()).add(str(r.MATNR))
    return out


# ---------------------------------------------------------------- persist / apply


def save(lp: LogisticsPlan, db: Db) -> None:
    now = db_ts()
    upsert_many(db, "FF_AG_SHIPMENT_PLAN", ("RUN_ID", "EBELN", "EBELP"), [{
        "RUN_ID": lp.run_id, "EBELN": s.ebeln, "EBELP": s.ebelp, "MATNR": s.material, "ORIGIN": s.origin,
        "OLD_ETA": s.old_eta.isoformat(), "NEW_ETA": s.chosen.new_eta.isoformat(), "MODE": s.chosen.mode,
        "CARRIER": s.chosen.carrier, "ROUTE_LABEL": s.chosen.label[:200], "EXTRA_COST_INR": float(s.chosen.extra_cost_inr),
        "SECURITY_RISK": int(s.chosen.security_risk), "STATUS": s.status, "DECISION_ID": s.decision_id,
        "ACTION_JSON": s.model_dump_json(), "CREATED_AT": now} for s in lp.shipments])
    upsert_many(db, "FF_AG_NOTIFICATION", ("NOTIFICATION_ID",), [{
        "NOTIFICATION_ID": n.notification_id, "RUN_ID": lp.run_id, "AUDIENCE": n.audience,
        "RECIPIENT": n.recipient[:120], "SUBJECT": n.subject, "BODY": n.body, "STATUS": n.status,
        "DECISION_ID": n.decision_id, "CREATED_AT": now} for n in lp.notifications])


def apply_decision(run_id: str, decision_id: str, option_id: str | None, approved: bool, db: Db) -> dict[str, Any]:
    """Execute (or cancel) a human-gated routing. Approving an alternative switches the booked option."""
    df = try_query(db, "SELECT EBELN, EBELP, ACTION_JSON FROM FF_AG_SHIPMENT_PLAN WHERE RUN_ID = ? AND DECISION_ID = ?",
                   (run_id, decision_id))
    if df is None or df.empty:
        raise ValueError(f"no shipment plan for decision {decision_id}")
    action = ShipmentAction.model_validate_json(str(df.iat[0, 2]))
    if approved and option_id and option_id != action.chosen.option_id:
        pick = next((a for a in action.alternatives if a.option_id == option_id), None)
        if pick is None:
            raise ValueError(f"option {option_id} is not offered for {decision_id}")
        action = action.model_copy(update={"alternatives": [action.chosen, *[a for a in action.alternatives
                                                                            if a.option_id != option_id]],
                                           "chosen": pick})
    status = "EXECUTED" if approved else "REJECTED"
    action = action.model_copy(update={"status": status})
    ch = action.chosen
    db.execute("UPDATE FF_AG_SHIPMENT_PLAN SET STATUS = ?, NEW_ETA = ?, MODE = ?, CARRIER = ?, ROUTE_LABEL = ?, "
               "EXTRA_COST_INR = ?, SECURITY_RISK = ?, ACTION_JSON = ? WHERE RUN_ID = ? AND EBELN = ? AND EBELP = ?",
               (status, ch.new_eta.isoformat(), ch.mode, ch.carrier, ch.label[:200], float(ch.extra_cost_inr),
                int(ch.security_risk), action.model_dump_json(), run_id, action.ebeln, action.ebelp))
    db.execute("UPDATE FF_AG_NOTIFICATION SET STATUS = ? WHERE RUN_ID = ? AND DECISION_ID = ?",
               ("QUEUED" if approved else "CANCELLED", run_id, decision_id))
    return {"ebeln": action.ebeln, "ebelp": action.ebelp, "status": status, "chosen": ch.model_dump(mode="json")}


def shipments(db: Db, run_id: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT RUN_ID, EBELN, EBELP, MATNR, ORIGIN, OLD_ETA, NEW_ETA, MODE, CARRIER, ROUTE_LABEL, EXTRA_COST_INR, "
           "SECURITY_RISK, STATUS, DECISION_ID FROM FF_AG_SHIPMENT_PLAN")
    df = try_query(db, sql + (" WHERE RUN_ID = ?" if run_id else "") + " ORDER BY CREATED_AT DESC, EBELN",
                   (run_id,) if run_id else ())
    return [] if df is None or df.empty else json.loads(df.to_json(orient="records", date_format="iso"))


def notifications(db: Db, run_id: str) -> list[dict[str, Any]]:
    df = try_query(db, f"SELECT NOTIFICATION_ID, AUDIENCE, RECIPIENT, SUBJECT, {lob_cols(db, 'BODY')}, STATUS, "
                       "DECISION_ID FROM FF_AG_NOTIFICATION WHERE RUN_ID = ? ORDER BY NOTIFICATION_ID", (run_id,))
    if df is None or df.empty:
        return []
    df["BODY"] = [lob_value(r, "BODY") for _, r in df.iterrows()]
    df = df.drop(columns=["BODY_S", "BODY_L"])
    return df.where(df.notna(), None).to_dict(orient="records")
