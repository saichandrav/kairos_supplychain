"""Compliance & Audit Agent: governs every disruption-response decision (deliberately LLM-free).

  * Classifies each proposed action AUTO / HUMAN / BLOCKED against rules/disruption.yaml governance:
      SANCTIONS         origin country on the sanctioned list                        -> FAIL (blocked)
      VENDOR_BLOCK      supplier has a posting block in the vendor master (SPERR)    -> FAIL (blocked)
      SECURITY_RISK     route transits a chokepoint with an elevated security alert  -> WARN (human)
      COST_THRESHOLD    single action above governance.human_cost_threshold_inr      -> WARN (human)
      RUN_BUDGET        autonomous spend in the run above governance.auto_run_budget -> WARN (human)
      COLD_CHAIN        transfer of 2-8 C stock needs a validated cold box           -> WARN (human)
      TRANSFER_SIZE     transfer above inventory.auto_transfer_max_units             -> WARN (human)
      SUPPLIER_SWITCH   any purchase / primary-supplier change                       -> always the Chief Pharmacist gate
  * Logs every decision with its rationale, checks, options and data sources to FF_AG_DECISION and appends a
    hash-chained FF_AG_AUDIT_LOG row (via A6), so the chain proves nothing was altered afterwards.
  * Resolves human decisions: an agent identity can never approve (governance.approver_blocklist_prefixes).
  * Builds the audit-ready report for a run on demand.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from agents import a6_audit as a6
from agents.common import db_ts, lob_cols, lob_value, try_query, upsert, upsert_many
from agents.contracts import (
    AuditEvent, Autonomy, CheckStatus, ComplianceCheck, DataSourceRef, Decision, DecisionOption, DecisionStatus,
    DecisionType, DisruptionAssessment, DisruptionSignalIn, InventoryPlan, LogisticsPlan, Overall, Recommendation,
    Scenario, ScenarioPlan,
)
from agents.ctx import Db
from agents.rules import Rules

AGENT = "DX5_COMPLIANCE"
HITL_AGENT = "HITL_DECISION"


def _g(rules: Rules, path: str) -> Any:
    return rules.value("disruption", path)


def _money(x: float) -> Decimal:
    return Decimal(f"{x:.4f}")


def autonomy_of(checks: Sequence[ComplianceCheck]) -> Autonomy:
    st = {c.status for c in checks}
    return Autonomy.BLOCKED if CheckStatus.FAIL in st else Autonomy.HUMAN if CheckStatus.WARN in st else Autonomy.AUTO


def _status_of(a: Autonomy) -> DecisionStatus:
    return {Autonomy.AUTO: DecisionStatus.EXECUTED, Autonomy.HUMAN: DecisionStatus.PENDING,
            Autonomy.BLOCKED: DecisionStatus.BLOCKED}[a]


class Governor:
    """Per-run guardrails: tracks autonomous spend against the run budget."""

    def __init__(self, rules: Rules, db: Db):
        self.rules = rules
        self.db = db
        self.threshold = float(_g(rules, "governance.human_cost_threshold_inr"))
        self.budget = float(_g(rules, "governance.auto_run_budget_inr"))
        self.sanctioned = {str(c).upper() for c in _g(rules, "governance.sanctioned_countries")}
        self.auto_spent = 0.0
        vend = try_query(db, "SELECT LIFNR, SPERR FROM FF_MM_LFA1")
        self.blocked_vendors = set() if vend is None else \
            {str(r.LIFNR) for r in vend.itertuples() if str(r.SPERR or "").strip().upper() == "X"}

    def cost_checks(self, cost: float) -> list[ComplianceCheck]:
        out = [ComplianceCheck(check_id="COST_THRESHOLD",
                               status=CheckStatus.WARN if cost > self.threshold else CheckStatus.PASS,
                               detail=f"INR {cost:.0f} vs human threshold INR {self.threshold:.0f}")]
        after = self.auto_spent + cost
        out.append(ComplianceCheck(check_id="RUN_BUDGET", status=CheckStatus.WARN if after > self.budget else CheckStatus.PASS,
                                   detail=f"autonomous spend would reach INR {after:.0f} of INR {self.budget:.0f}"))
        return out

    def commit(self, d: Decision) -> Decision:
        if d.autonomy is Autonomy.AUTO and d.cost_inr is not None:
            self.auto_spent += float(d.cost_inr)
        return d


# ---------------------------------------------------------------- decisions per agent


def triage_decision(signal: DisruptionSignalIn, a: DisruptionAssessment, run_id: str) -> Decision:
    """Ambiguous signal -> a human analyst confirms how to treat it, or dismisses it."""
    routes = ", ".join(sorted({r.disrupted_chokepoint or r.origin_name for r in a.affected_sea_routes})) or "none matched"
    return Decision(
        decision_id=f"{run_id}:TRIAGE", run_id=run_id, agent="DX1_SENSING", decision_type=DecisionType.SIGNAL_TRIAGE,
        subject=f"Triage signal {signal.signal_id}: {a.summary}"[:200], autonomy=Autonomy.HUMAN,
        status=DecisionStatus.PENDING,
        rationale=(f"Escalated to a human analyst: {a.escalation_reason}. LLM view: {a.event_type.value}, "
                   f"{a.severity.value}, confidence {a.confidence:.2f}. Lanes matched: {routes}.")[:4000],
        options=[
            DecisionOption(option_id="CONFIRM_RISK", label="Confirm: elevated risk, routes stay open",
                           detail={"signal_id": signal.signal_id}),
            DecisionOption(option_id="CONFIRM_CLOSURE", label="Confirm: closure of the named chokepoints/ports",
                           detail={"signal_id": signal.signal_id}),
            DecisionOption(option_id="DISMISS", label="Dismiss: not a supply-chain disruption",
                           detail={"signal_id": signal.signal_id}),
        ],
        recommended_option="CONFIRM_CLOSURE" if a.severity.value == "CRITICAL" else "CONFIRM_RISK",
        human_reasons=[a.escalation_reason or "ambiguous signal"],
        data_sources=[DataSourceRef(source=signal.source[:100], source_url=signal.source_url, is_proxy="PROXY")])


def plan_decision(plan: ScenarioPlan, run_id: str) -> Decision:
    rec = next((o for o in plan.options if o.option_id == plan.recommended_option), None)
    return Decision(
        decision_id=f"{run_id}:PLAN", run_id=run_id, agent="DX2_SCENARIO_PLAN", decision_type=DecisionType.SCENARIO_PLAN,
        subject=f"Adopt response plan {plan.plan_id}", autonomy=Autonomy.AUTO, status=DecisionStatus.EXECUTED,
        rationale=(plan.narrative or "")[:4000],
        options=[DecisionOption(option_id=o.option_id, label=o.label, cost_inr=o.cost_inr,
                                detail={"stockout_units": o.stockout_units, "time_to_effect_days": o.time_to_effect_days,
                                        "residual_risk": o.residual_risk, "score": o.score}) for o in plan.options],
        recommended_option=plan.recommended_option, chosen_option=plan.recommended_option,
        cost_inr=rec.cost_inr if rec else None, data_sources=plan.data_sources)


def review_shipments(lp: LogisticsPlan, gov: Governor) -> list[Decision]:
    """Classify each routing; sets shipment status/decision_id and holds partner notices of gated routings."""
    decisions = []
    for s in lp.shipments:
        ch = s.chosen
        cost = float(ch.extra_cost_inr)
        checks = [
            ComplianceCheck(check_id="SANCTIONS",
                            status=CheckStatus.FAIL if (s.origin or "").upper() in gov.sanctioned else CheckStatus.PASS,
                            detail=f"origin {s.origin} checked against {sorted(gov.sanctioned)}"),
            ComplianceCheck(check_id="VENDOR_BLOCK",
                            status=CheckStatus.FAIL if s.supplier in gov.blocked_vendors else CheckStatus.PASS,
                            detail=f"supplier {s.supplier} posting block (LFA1.SPERR)"),
            ComplianceCheck(check_id="SECURITY_RISK", status=CheckStatus.WARN if ch.security_risk else CheckStatus.PASS,
                            detail=("route transits " + ", ".join(ch.via) + " under an elevated security alert")
                            if ch.security_risk else "route avoids chokepoints under security alert"),
            *gov.cost_checks(cost),
        ]
        auto = autonomy_of(checks)
        reasons = [c.detail for c in checks if c.status is not CheckStatus.PASS]
        d = gov.commit(Decision(
            decision_id=f"{lp.run_id}:SHP:{s.ebeln}-{s.ebelp}", run_id=lp.run_id, agent="DX3_LOGISTICS",
            decision_type=DecisionType.SHIPMENT_ROUTING,
            subject=f"PO {s.ebeln}/{s.ebelp} {s.material}: {ch.label} via {ch.carrier}"[:200],
            autonomy=auto, status=_status_of(auto),
            rationale=(f"Lowest objective (extra freight + late-unit penalty) among {1 + len(s.alternatives)} feasible "
                       f"options for the {s.origin} lane: ETA {s.old_eta} -> {ch.new_eta} (+{ch.delay_days} days), "
                       f"extra INR {cost:.0f}, {ch.late_units} units after projected stock-out on {s.stockout_date}."),
            options=[DecisionOption(option_id=o.option_id, label=f"{o.label} ({o.mode}, {o.carrier})",
                                    cost_inr=o.extra_cost_inr,
                                    detail={"new_eta": o.new_eta.isoformat(), "delay_days": o.delay_days,
                                            "late_units": o.late_units, "security_risk": o.security_risk})
                     for o in [ch, *s.alternatives]],
            recommended_option=ch.option_id, chosen_option=ch.option_id if auto is Autonomy.AUTO else None,
            cost_inr=ch.extra_cost_inr, checks=checks, human_reasons=reasons,
            data_sources=[DataSourceRef(source="FF_MM_EKPO", is_proxy="SYNTH"),
                          DataSourceRef(source="rules/sea_routes.yaml", is_proxy="PROXY")]))
        s.decision_id, s.status = d.decision_id, d.status.value
        decisions.append(d)
    by_po = {f"PO {s.ebeln}/{s.ebelp}": s for s in lp.shipments}
    for n in lp.notifications:
        if n.audience == "SUPPLIER":
            s = by_po.get(n.subject.split(":")[0])
            if s:
                n.decision_id = s.decision_id
                n.status = "QUEUED" if s.status == DecisionStatus.EXECUTED.value else "HELD"
        elif n.audience == "FORWARDER" and any(s.status != DecisionStatus.EXECUTED.value
                                                for s in lp.shipments if s.chosen.carrier == n.recipient):
            n.status = "HELD"
    return decisions


def review_transfers(ip: InventoryPlan, gov: Governor) -> list[Decision]:
    max_units = float(gov.rules.value("disruption", "inventory.auto_transfer_max_units"))
    decisions = []
    for t in ip.transfers:
        checks = [
            ComplianceCheck(check_id="COLD_CHAIN", status=CheckStatus.WARN if t.cold_chain else CheckStatus.PASS,
                            detail="2-8 C stock: validated cold box and temperature log required" if t.cold_chain
                            else "ambient stock"),
            ComplianceCheck(check_id="TRANSFER_SIZE", status=CheckStatus.WARN if t.qty > max_units else CheckStatus.PASS,
                            detail=f"{t.qty:g} units vs auto limit {max_units:g}"),
        ]
        auto = autonomy_of(checks)
        d = gov.commit(Decision(
            decision_id=t.transfer_id, run_id=ip.run_id, agent="DX4_INVENTORY", decision_type=DecisionType.STOCK_TRANSFER,
            subject=f"Move {t.qty:g} {t.material} {t.from_location} -> {t.to_location}",
            autonomy=auto, status=_status_of(auto),
            rationale=(f"{t.to_location} cover {t.cover_to_before} days is below the hospital average; transfer lifts "
                       f"it to {t.cover_to_after} days while {t.from_location} keeps {t.cover_from_after} days."),
            options=[DecisionOption(option_id="TRANSFER", label=f"Transfer {t.qty:g} units (movement 311)"),
                     DecisionOption(option_id="HOLD", label="Do not transfer")],
            recommended_option="TRANSFER", chosen_option="TRANSFER" if auto is Autonomy.AUTO else None,
            cost_inr=_money(0), checks=checks, human_reasons=[c.detail for c in checks if c.status is not CheckStatus.PASS],
            data_sources=[DataSourceRef(source="FF_V_BATCH_FEFO", is_proxy="SYNTH"),
                          DataSourceRef(source="FF_MM_MSEG", is_proxy="SYNTH")]))
        t.decision_id, t.status = d.decision_id, d.status.value
        decisions.append(d)
    return decisions


def safety_stock_decision(ip: InventoryPlan) -> Decision | None:
    if not ip.safety_stock:
        return None
    up = [s for s in ip.safety_stock if s.safety_stock_new > s.safety_stock_old]
    return Decision(
        decision_id=f"{ip.run_id}:SS", run_id=ip.run_id, agent="DX4_INVENTORY",
        decision_type=DecisionType.SAFETY_STOCK_UPDATE,
        subject=f"Recompute safety stock / reorder point for {len(ip.safety_stock)} materials",
        autonomy=Autonomy.AUTO, status=DecisionStatus.EXECUTED,
        rationale=(f"Lead time extended by the disruption; {len(up)} materials need more safety stock. "
                   "SS = z(VED) x sqrt((L + delay) x sd^2 + daily^2 x (k x delay)^2); recommended planning parameters "
                   "only (no stock is bought by this decision)."),
        options=[DecisionOption(option_id=s.material, label=f"{s.material}: SS {s.safety_stock_old:g} -> "
                                                            f"{s.safety_stock_new:g}, ROP {s.reorder_point_new:g}")
                 for s in ip.safety_stock[:25]],
        chosen_option="APPLY", data_sources=[DataSourceRef(source="FF_MM_MSEG", is_proxy="SYNTH")])


def procurement_decisions(recs: Sequence[Recommendation], scenarios: Sequence[Scenario], run_id: str) -> list[Decision]:
    """Every purchase goes to the Chief Pharmacist gate (/approval + checklist), never autonomously."""
    by_id = {s.scenario_id: s for s in scenarios}
    out = []
    for r in recs:
        s = by_id[r.scenario_id]
        blocked = r.overall is Overall.BLOCKED
        fails = [c.check_id for c in r.checks if c.status is CheckStatus.FAIL]
        out.append(Decision(
            decision_id=f"PR:{r.scenario_id}"[:60], run_id=run_id, agent="A5_VALIDATOR",
            decision_type=DecisionType.PROCUREMENT_PROPOSAL,
            subject=f"{s.option_type.value} {s.qty:g} x {s.material or s.formulation_id} from {s.supplier}"[:200],
            autonomy=Autonomy.BLOCKED if blocked else Autonomy.HUMAN,
            status=DecisionStatus.BLOCKED if blocked else DecisionStatus.PENDING,
            rationale=(f"A4 rank {s.rank} option covering {s.coverage_days:g} days at INR {float(s.cost):.0f}; A5 "
                       f"overall {r.overall.value}" + (f" (failed: {', '.join(fails)})" if fails else "")
                       + ". Purchases always need the Chief Pharmacist checklist; A6 posts the PR only after approval."),
            options=[DecisionOption(option_id=r.scenario_id, label="Approve via the Chief Pharmacist gate",
                                    cost_inr=s.cost)],
            recommended_option=r.scenario_id, cost_inr=s.cost,
            checks=[ComplianceCheck(check_id=c.check_id, status=c.status, detail=c.name) for c in r.checks],
            human_reasons=["purchase / supplier change requires the Chief Pharmacist"],
            data_sources=[DataSourceRef(source="FF_AG_RECOMMENDATION", is_proxy="SYNTH")]))
    return out


# ---------------------------------------------------------------- persistence + audit chain


def _decision_row(d: Decision) -> dict[str, Any]:
    return {
        "DECISION_ID": d.decision_id, "RUN_ID": d.run_id, "AGENT": d.agent[:20], "DECISION_TYPE": d.decision_type.value,
        "SUBJECT": d.subject, "AUTONOMY": d.autonomy.value, "STATUS": d.status.value,
        "COST_INR": float(d.cost_inr) if d.cost_inr is not None else None,
        "RECOMMENDED_OPTION": d.recommended_option, "CHOSEN_OPTION": d.chosen_option, "DECIDED_BY": d.decided_by,
        "DECIDED_AT": db_ts(d.decided_at) if d.decided_at else None, "DECISION_REASON": d.decision_reason,
        "DECISION_JSON": d.model_dump_json(), "CREATED_AT": db_ts(d.created_at),
    }


def _audit_event(d: Decision, rules_versions: dict[str, str] | None) -> AuditEvent:
    return AuditEvent(
        run_id=d.run_id, seq=0, agent=(HITL_AGENT if d.decided_by else d.agent)[:20],
        input_hash=a6.hash_data({"decision_id": d.decision_id, "options": [o.model_dump(mode="json") for o in d.options],
                                 "checks": [c.model_dump(mode="json") for c in d.checks], "rationale": d.rationale}),
        output_hash=a6.hash_data({"decision_id": d.decision_id, "autonomy": d.autonomy.value, "status": d.status.value,
                                  "chosen": d.chosen_option, "by": d.decided_by, "reason": d.decision_reason}),
        rule_versions=rules_versions or {}, data_sources=d.data_sources[:10])


def record(d: Decision, db: Db, rules_versions: dict[str, str] | None = None) -> Decision:
    """Upsert FF_AG_DECISION and append a hash-chained audit row for the decision."""
    upsert(db, "FF_AG_DECISION", ("DECISION_ID",), _decision_row(d))
    a6.audit(_audit_event(d, rules_versions), db)
    return d


def audit_decisions(ds: Sequence[Decision], chain: a6.Chain, rules_versions: dict[str, str]) -> None:
    """Add the decisions' audit rows to the run's chain (written on chain.flush())."""
    for d in ds:
        chain.add(_audit_event(d, rules_versions))


def save_decisions(ds: Sequence[Decision], db: Db) -> None:
    upsert_many(db, "FF_AG_DECISION", ("DECISION_ID",), [_decision_row(d) for d in ds])


def load(decision_id: str, db: Db) -> Decision | None:
    ds = decisions(db, decision_id=decision_id)
    return ds[0] if ds else None


def decisions(db: Db, run_id: str | None = None, status: str | None = None, limit: int = 200,
              decision_id: str | None = None) -> list[Decision]:
    where, params = [], []
    for col, val in (("DECISION_ID", decision_id), ("RUN_ID", run_id), ("STATUS", status)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    sql = f"SELECT {lob_cols(db, 'DECISION_JSON')} FROM FF_AG_DECISION" + (f" WHERE {' AND '.join(where)}" if where else "")
    df = try_query(db, sql + f" ORDER BY CREATED_AT DESC, DECISION_ID LIMIT {int(limit)}", params)
    return [] if df is None else [Decision.model_validate_json(lob_value(r, "DECISION_JSON")) for _, r in df.iterrows()]


def check_approver(approver: str, rules: Rules) -> str:
    who = (approver or "").strip()
    low = who.lower()
    if not who or any(low == p or low.startswith((p + ":", p + "-", p + "_", p + "@")) or low.startswith(p + " ")
                       for p in _g(rules, "governance.approver_blocklist_prefixes")):
        raise PermissionError("a human approver identity is required; agents cannot resolve decisions")
    return who


def resolve(decision_id: str, approver: str, approve: bool, option_id: str | None, reason: str | None,
            db: Db, rules: Rules) -> Decision:
    """Record a human's decision on a PENDING item (chain-audited). Execution is done by the owning agent."""
    who = check_approver(approver, rules)
    d = load(decision_id, db)
    if d is None:
        raise ValueError(f"decision {decision_id} not found")
    if d.status is not DecisionStatus.PENDING:
        raise PermissionError(f"decision {decision_id} is {d.status.value}, not PENDING")
    if d.decision_type is DecisionType.PROCUREMENT_PROPOSAL:
        raise PermissionError("purchases are approved through the Chief Pharmacist gate (/approval with checklist)")
    if not approve and not (reason and reason.strip()):
        raise ValueError("a rejection needs a reason")
    chosen = option_id or d.recommended_option
    if approve and chosen not in {o.option_id for o in d.options}:
        raise ValueError(f"option {chosen} is not offered for {decision_id}")
    d = d.model_copy(update={
        "status": DecisionStatus.APPROVED if approve else DecisionStatus.REJECTED,
        "chosen_option": chosen if approve else None, "decided_by": who,
        "decided_at": datetime.now(timezone.utc), "decision_reason": (reason or "").strip()[:1000] or None})
    return record(d, db, rules.versions)


def sync_procurement(scenario_id: str, action: str, approver: str, db: Db) -> None:
    """Mirror a Chief Pharmacist gate decision onto the disruption decision log (no-op for other runs)."""
    d = load(f"PR:{scenario_id}"[:60], db)
    if d is None or d.status is not DecisionStatus.PENDING:
        return
    approved = action in ("APPROVE", "EDIT_APPROVE")
    d = d.model_copy(update={"status": DecisionStatus.APPROVED if approved else DecisionStatus.REJECTED,
                             "chosen_option": scenario_id if approved else None, "decided_by": approver,
                             "decided_at": datetime.now(timezone.utc), "decision_reason": f"Chief Pharmacist gate: {action}"})
    record(d, db)


# ---------------------------------------------------------------- report


def report(run_id: str, db: Db) -> dict[str, Any]:
    """Audit-ready JSON dossier for a disruption response run."""
    run = try_query(db, "SELECT * FROM FF_AG_RUN WHERE RUN_ID = ?", (run_id,))
    if run is None or run.empty:
        raise ValueError(f"run {run_id} not found")
    sig = try_query(db, "SELECT SIGNAL_ID, SOURCE, SOURCE_URL, EVENT_TYPE, SEVERITY, CONFIDENCE, LOCATION, SUMMARY, "
                        "STATUS, MODEL_ID FROM FF_AG_DISRUPTION WHERE RUN_ID = ?", (run_id,))
    plan = try_query(db, "SELECT PLAN_JSON FROM FF_AG_WHATIF WHERE RUN_ID = ? ORDER BY CREATED_AT DESC", (run_id,))
    audit = try_query(db, "SELECT SEQ, AGENT, TS, INPUT_HASH, OUTPUT_HASH, PREV_HASH, ROW_HASH, DATA_SOURCES_JSON "
                          "FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ? ORDER BY SEQ", (run_id,))
    valid, err = a6.verify_chain(run_id, db)
    ds = decisions(db, run_id=run_id, limit=1000)
    plan_obj = ScenarioPlan.model_validate_json(str(plan.iat[0, 0])) if plan is not None and not plan.empty else None
    counts: dict[str, int] = {}
    for d in ds:
        counts[d.status.value] = counts.get(d.status.value, 0) + 1
    return {
        "run": json.loads(run.iloc[:1].to_json(orient="records", date_format="iso"))[0],
        "signal": sig.where(sig.notna(), None).to_dict(orient="records")[0] if sig is not None and not sig.empty else None,
        "plan": plan_obj.model_dump(mode="json") if plan_obj else None,
        "decisions": [d.model_dump(mode="json") for d in sorted(ds, key=lambda d: (d.created_at, d.decision_id))],
        "decision_counts": counts,
        "audit_chain": {"valid": valid, "error": err,
                        "events": [] if audit is None else json.loads(audit.to_json(orient="records", date_format="iso"))},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def report_html(rep: dict[str, Any], shipments: list[dict[str, Any]], transfers: list[dict[str, Any]],
                notices: list[dict[str, Any]]) -> str:
    e = lambda x: html.escape("" if x is None else str(x))  # noqa: E731
    run, sig, plan = rep["run"], rep["signal"], rep["plan"]
    chain = rep["audit_chain"]

    def table(headers: list[str], rows: list[list[Any]]) -> str:
        head = "".join(f"<th>{e(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>" for r in rows)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body or '<tr><td colspan=99>none</td></tr>'}</tbody></table>"

    parts = [
        f"<h1>Disruption Response Audit Dossier</h1><p class=meta>Run {e(run['RUN_ID'])} · trigger "
        f"{e(run['TRIGGER_TYPE'])} · status {e(run['STATUS'])} · generated {e(rep['generated_at'])}</p>",
        f"<div class='chain {'ok' if chain['valid'] else 'bad'}'>Audit hash chain: "
        f"{'VERIFIED' if chain['valid'] else 'BROKEN — ' + e(chain['error'])} ({len(chain['events'])} events)</div>",
    ]
    if sig:
        parts.append("<h2>1. Signal (Disruption Sensing)</h2>" + table(
            ["Signal", "Source", "Type", "Severity", "Confidence", "Location", "Summary", "Model"],
            [[sig["SIGNAL_ID"], sig["SOURCE"], sig["EVENT_TYPE"], sig["SEVERITY"], sig["CONFIDENCE"], sig["LOCATION"],
              sig["SUMMARY"], sig["MODEL_ID"] or "deterministic fallback"]]))
    if plan:
        parts.append("<h2>2. Scenario plan</h2><p>" + e(plan.get("narrative")) + "</p>" + table(
            ["Option", "Cost INR", "Stock-out units", "Time (days)", "Residual risk", "Score"],
            [[o["label"], f"{float(o['cost_inr']):.0f}", o["stockout_units"], o["time_to_effect_days"],
              o["residual_risk"], o["score"]] for o in plan["options"]]))
    parts.append("<h2>3. Logistics re-plan</h2>" + table(
        ["PO", "Material", "Origin", "Old ETA", "New ETA", "Mode", "Carrier", "Route", "Extra INR", "Status"],
        [[f"{s['EBELN']}/{s['EBELP']}", s["MATNR"], s["ORIGIN"], s["OLD_ETA"], s["NEW_ETA"], s["MODE"], s["CARRIER"],
          s["ROUTE_LABEL"], f"{float(s['EXTRA_COST_INR'] or 0):.0f}", s["STATUS"]] for s in shipments]))
    parts.append("<h2>4. Inventory transfers</h2>" + table(
        ["Transfer", "Material", "From", "To", "Qty", "Cold chain", "Status"],
        [[t["TRANSFER_ID"], t["MATNR"], t["FROM_LGORT"], t["TO_LGORT"], t["QTY"], bool(t["COLD_CHAIN"]), t["STATUS"]]
         for t in transfers]))
    parts.append("<h2>5. Decisions (autonomous and human)</h2>" + table(
        ["Decision", "Agent", "Type", "Autonomy", "Status", "Cost INR", "Decided by", "Rationale / checks"],
        [[d["decision_id"], d["agent"], d["decision_type"], d["autonomy"], d["status"],
          "" if d["cost_inr"] is None else f"{float(d['cost_inr']):.0f}", d["decided_by"] or "agent (within policy)",
          d["rationale"] + (" | " + "; ".join(f"{c['check_id']}={c['status']}" for c in d["checks"]) if d["checks"] else "")]
         for d in rep["decisions"]]))
    parts.append("<h2>6. Partner notices</h2>" + table(
        ["Audience", "Recipient", "Subject", "Status"],
        [[n["AUDIENCE"], n["RECIPIENT"], n["SUBJECT"], n["STATUS"]] for n in notices]))
    parts.append("<h2>7. Hash-chained audit log</h2>" + table(
        ["Seq", "Agent", "Timestamp", "Prev hash", "Row hash"],
        [[a["SEQ"], a["AGENT"], a["TS"], (a["PREV_HASH"] or "")[:16], (a["ROW_HASH"] or "")[:16]] for a in chain["events"]]))
    parts.append("<p class=meta>All figures are planning estimates from rules/*.yaml over SYNTH/PROXY data and are "
                 "probabilistic flags for human review. Purchases are posted to S/4 only after a Chief Pharmacist "
                 "approval row exists (A6).</p>")
    style = ("body{font-family:Inter,Arial,sans-serif;margin:32px;color:#0f172a;background:#fff}h1{margin:0 0 4px}"
             "h2{margin-top:28px;border-bottom:2px solid #e2e8f0;padding-bottom:4px;font-size:1.05rem}"
             "table{border-collapse:collapse;width:100%;font-size:12px;margin-top:8px}th,td{border:1px solid #e2e8f0;"
             "padding:5px 7px;text-align:left;vertical-align:top}th{background:#f1f5f9}.meta{color:#64748b;font-size:12px}"
             ".chain{margin-top:12px;padding:8px 12px;border-radius:6px;font-weight:600}.ok{background:#ecfdf5;"
             "color:#059669}.bad{background:#fef2f2;color:#dc2626}@media print{body{margin:10mm}}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Disruption dossier {e(run['RUN_ID'])}</title>"
            f"<style>{style}</style></head><body>{''.join(parts)}</body></html>")
