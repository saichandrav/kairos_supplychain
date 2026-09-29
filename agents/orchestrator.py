"""Kairos Pipeline Orchestrator (plan §2, §3, §13 S08).

Sequences agents A1 → A2 → A3 → A4 → A5, records audit hashes at each hop,
persists outputs to HANA/SQLite, and stops at the Human Approval Gate.
Provides `decide()` to process Chief Pharmacist actions and trigger A6 S/4 PR generation.
"""
from __future__ import annotations

import argparse
import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Sequence

from agents import (
    a1_margin_sentinel as a1,
    a2_dependency as a2,
    a3_forecast as a3,
    a4_resilience as a4,
    a5_validator as a5,
    a6_audit as a6,
    compliance,
    disruption_sensing as sensing,
    inventory_rebalance as inventory,
    logistics,
    scenario_planning as planning,
)
from agents.common import (
    AgentCtx, as_of_date, db_ts, placeholders, read_replica, resolve_ctx, try_query, upsert,
)
from agents.contracts import (
    ApprovalAction, ApprovalChecklist, ApprovalDecision, AuditEvent,
    DataSourceRef, DecisionStatus, DecisionType, DependencyProfile, DisruptionSignalIn, Forecast, OptionType, Overall,
    Recommendation, RiskSignal, RunContext, RunTrigger, Scenario, ScenarioPlan, ShockSpec, SignalBand, WhatIfSpec,
)
from agents.ctx import Db, Settings, get_db, load_settings
from agents.rules import Rules, load_rules

log = logging.getLogger(__name__)

RUN_COLS = (
    "RUN_ID", "STARTED_AT", "FINISHED_AT", "TRIGGER_TYPE", "STATUS",
    "FORMULATION_IDS_JSON", "AS_OF_DATE", "SHOCK_JSON", "DRY_RUN",
    "RULE_VERSIONS_JSON",
)

APPROVAL_COLS = (
    "RUN_ID", "SCENARIO_ID", "LEVEL_NO", "DECIDED_AT", "APPROVER",
    "ACTION", "CHECKLIST_JSON", "REASON", "EDITED_QTY", "EDITED_SUPPLIER",
    "SNOOZE_DAYS",
)


def _audit_hop(run_id: str, agent_name: str, input_data: Any, output_data: Any,
               sources: list[DataSourceRef], rules: Rules, db: Db) -> None:
    """Record audit event for a completed agent hop."""
    event = AuditEvent(
        run_id=run_id,
        seq=0,  # a6.audit assigns consecutive sequence
        agent=agent_name,
        ts=datetime.now(timezone.utc),
        input_hash=a6.hash_data(input_data),
        output_hash=a6.hash_data(output_data),
        rule_versions=rules.versions,
        data_sources=sources,
    )
    a6.audit(event, db)


def run(
    form_ids: Sequence[str] | None = None,
    shock: float = 1.0,
    ctx: AgentCtx | None = None,
    trigger: RunTrigger = RunTrigger.ON_DEMAND,
    as_of: date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute end-to-end multi-agent pipeline up to the human approval gate."""
    c = resolve_ctx(ctx, "run")
    db = c.db
    rules = c.rules

    run_id = f"ff-{uuid.uuid4().hex[:10]}"
    as_of_dt = as_of or c.run.as_of or date.today()
    form_list = list(form_ids) if form_ids else []

    shock_spec = ShockSpec(api_cost_pct=(shock - 1.0) * 100.0, label=f"x{shock:g}") if shock != 1.0 else None
    run_ctx = RunContext(
        run_id=run_id,
        started_at=datetime.now(timezone.utc),
        trigger=trigger,
        formulation_ids=form_list,
        as_of=as_of_dt,
        shock=shock_spec,
        dry_run=dry_run,
        rule_versions=rules.versions,
    )
    c.run = run_ctx

    now_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        db.execute(
            f"INSERT INTO FF_AG_RUN ({', '.join(RUN_COLS)}) VALUES ({placeholders(RUN_COLS)})",
            (
                run_id, now_str, None, trigger.value, "RUNNING",
                json.dumps(form_list), as_of_dt.isoformat(),
                json.dumps(shock_spec.model_dump(mode="json")) if shock_spec else None,
                int(dry_run), json.dumps(rules.versions)
            )
        )

    # 1. A1 Margin Sentinel
    signals: list[RiskSignal] = a1.run(form_list or None, c, api_cost_multiplier=shock)
    if not dry_run:
        _audit_hop(
            run_id, "A1_MARGIN_SENTINEL",
            {"form_ids": form_list, "shock": shock},
            [s.model_dump(mode="json") for s in signals],
            [DataSourceRef(source="FF_REF_CEILING_PRICE", is_proxy="REAL"),
             DataSourceRef(source="FF_REF_API_COST_MONTHLY", is_proxy="PROXY")],
            rules, db
        )

    # Target formulation ids determined by A1 or requested
    active_forms = [s.formulation_id for s in signals] if not form_list else form_list

    # 2. A2 Dependency Mapper
    deps: list[DependencyProfile] = a2.run(active_forms, c)
    if not dry_run:
        _audit_hop(
            run_id, "A2_DEPENDENCY",
            {"form_ids": active_forms},
            [d.model_dump(mode="json") for d in deps],
            [DataSourceRef(source="FF_REF_PRODUCER", is_proxy="REAL"),
             DataSourceRef(source="FF_REF_API_ORIGIN", is_proxy="PROXY")],
            rules, db
        )

    # 3. A3 Shortage Forecaster
    forecasts: list[Forecast] = a3.run(signals, deps, c)
    if not dry_run:
        _audit_hop(
            run_id, "A3_FORECASTER",
            {"signals_count": len(signals), "deps_count": len(deps)},
            [f.model_dump(mode="json") for f in forecasts],
            [DataSourceRef(source="FF_MM_MCHB", is_proxy="SYNTH"),
             DataSourceRef(source="FF_MM_MSEG", is_proxy="SYNTH")],
            rules, db
        )

    # 4. A4 Resilience Simulator
    scenarios: list[Scenario] = a4.run(forecasts, c)
    if not dry_run:
        _audit_hop(
            run_id, "A4_RESILIENCE",
            {"forecasts_count": len(forecasts)},
            [s.model_dump(mode="json") for s in scenarios],
            [DataSourceRef(source="FF_MM_MARA", is_proxy="SYNTH"),
             DataSourceRef(source="FF_MM_LFA1", is_proxy="SYNTH")],
            rules, db
        )

    # 5. A5 Policy Validator
    recommendations: list[Recommendation] = a5.run(scenarios, forecasts, c)
    if not dry_run:
        _audit_hop(
            run_id, "A5_VALIDATOR",
            {"scenarios_count": len(scenarios)},
            [r.model_dump(mode="json") for r in recommendations],
            [DataSourceRef(source="rules/dpco.yaml", is_proxy="REAL"),
             DataSourceRef(source="rules/sop_controls.yaml", is_proxy="REAL")],
            rules, db
        )

    finish_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        db.execute(
            "UPDATE FF_AG_RUN SET STATUS = ?, FINISHED_AT = ? WHERE RUN_ID = ?",
            ("AWAITING_APPROVAL", finish_str, run_id)
        )

    return {
        "run_id": run_id,
        "as_of": as_of_dt.isoformat(),
        "shock": shock,
        "status": "AWAITING_APPROVAL",
        "signals": signals,
        "dependencies": deps,
        "forecasts": forecasts,
        "scenarios": scenarios,
        "recommendations": recommendations,
    }


def decide(
    scenario_id: str,
    user: str,
    action: ApprovalAction | str,
    checklist: ApprovalChecklist | dict,
    run_id: str | None = None,
    reason: str | None = None,
    edited_qty: float | None = None,
    edited_supplier: str | None = None,
    snooze_days: int | None = None,
    db: Db | None = None,
) -> dict[str, Any]:
    """Record human approval decision and trigger A6 action if approved."""
    if db is None:
        db = get_db()

    act_enum = ApprovalAction(action) if isinstance(action, str) else action
    chk = ApprovalChecklist(**checklist) if isinstance(checklist, dict) else checklist

    # If run_id not provided, find it from recommendation
    if not run_id:
        r_df = try_query(db, "SELECT RUN_ID FROM FF_AG_RECOMMENDATION WHERE SCENARIO_ID = ?", (scenario_id,))
        if r_df is None or r_df.empty:
            raise ValueError(f"Scenario {scenario_id} not found in recommendations")
        run_id = str(r_df.iloc[0]["RUN_ID"])

    # Level check: does it require level 2 approver?
    rec_df = try_query(
        db,
        "SELECT NEEDS_SECOND_APPROVER FROM FF_AG_RECOMMENDATION WHERE RUN_ID = ? AND SCENARIO_ID = ?",
        (run_id, scenario_id)
    )
    needs_second = bool(rec_df.iloc[0]["NEEDS_SECOND_APPROVER"]) if rec_df is not None and not rec_df.empty else False
    level = 2 if needs_second else 1

    decision = ApprovalDecision(
        run_id=run_id,
        scenario_id=scenario_id,
        approver=user,
        level=level,
        action=act_enum,
        decided_at=datetime.now(timezone.utc),
        checklist=chk,
        reason=reason,
        edited_qty=edited_qty,
        edited_supplier=edited_supplier,
        snooze_days=snooze_days,
    )

    # Write to FF_AG_APPROVAL
    db.execute(
        f"INSERT INTO FF_AG_APPROVAL ({', '.join(APPROVAL_COLS)}) VALUES ({placeholders(APPROVAL_COLS)})",
        (
            decision.run_id, decision.scenario_id, decision.level,
            decision.decided_at.isoformat(timespec="seconds"), decision.approver,
            decision.action.value, json.dumps(decision.checklist.model_dump()),
            decision.reason, decision.edited_qty, decision.edited_supplier,
            decision.snooze_days
        )
    )

    action_result: dict[str, Any] | None = None
    if decision.approves:
        action_result = a6.act(scenario_id, db, run_id=run_id, approver=user)
    compliance.sync_procurement(scenario_id, decision.action.value, user, db)

    return {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "action": decision.action.value,
        "approver": decision.approver,
        "level": decision.level,
        "action_result": action_result,
        "status": "APPROVED_AND_ACTED" if decision.approves else decision.action.value,
    }


# ---------------------------------------------------------------- disruption response
#
# Disruption Sensing -> (escalate to analyst | monitor | plan) -> Scenario Planning -> Rerouting & Logistics
# -> Inventory Rebalancing -> A4 Resilience -> A5 Validator -> Compliance & Audit. Autonomous actions run only
# inside rules/disruption.yaml governance; everything else waits in FF_AG_DECISION for a human, and purchases
# always wait for the Chief Pharmacist gate (/approval) before A6 posts a PR.


def _dx_ctx(run_id: str, db: Db, rules: Rules, dry_run: bool = False) -> AgentCtx:
    return AgentCtx(db=db, rules=rules, run=RunContext(run_id=run_id, trigger=RunTrigger.DISRUPTION,
                                                       as_of=as_of_date(db), dry_run=dry_run,
                                                       rule_versions=rules.versions))


def _hop(chain: a6.Chain, agent: str, input_data: Any, output_data: Any, sources: list[DataSourceRef],
         rules: Rules) -> None:
    """Audit one agent hop on the run's chain (written on the next chain.flush())."""
    chain.add(AuditEvent(run_id=chain.run_id, seq=0, agent=agent, ts=datetime.now(timezone.utc),
                         input_hash=a6.hash_data(input_data), output_hash=a6.hash_data(output_data),
                         rule_versions=rules.versions, data_sources=sources))


def _dx_run_row(run_id: str, status: str, db: Db, rules: Rules, forms: Sequence[str] = (),
                specs: Sequence[WhatIfSpec] = (), finished: bool = False) -> None:
    """Create the FF_AG_RUN row for a disruption run, or update its status / scope if it exists."""
    forms_json = json.dumps(list(forms))[:2000]
    specs_json = json.dumps([s.model_dump(mode="json") for s in specs])[:1000] if specs else None
    done = db_ts() if finished else None
    exists = try_query(db, "SELECT RUN_ID FROM FF_AG_RUN WHERE RUN_ID = ?", (run_id,))
    if exists is not None and not exists.empty:
        db.execute("UPDATE FF_AG_RUN SET STATUS = ?, FORMULATION_IDS_JSON = COALESCE(?, FORMULATION_IDS_JSON), "
                   "SHOCK_JSON = COALESCE(?, SHOCK_JSON), FINISHED_AT = ? WHERE RUN_ID = ?",
                   (status, forms_json if forms else None, specs_json, done, run_id))
        return
    upsert(db, "FF_AG_RUN", ("RUN_ID",), {
        "RUN_ID": run_id, "STARTED_AT": db_ts(), "FINISHED_AT": done,
        "TRIGGER_TYPE": RunTrigger.DISRUPTION.value, "STATUS": status, "FORMULATION_IDS_JSON": forms_json,
        "AS_OF_DATE": as_of_date(db).isoformat(), "SHOCK_JSON": specs_json, "DRY_RUN": 0,
        "RULE_VERSIONS_JSON": json.dumps(rules.versions)})


def _dx_run_status(run_id: str, status: str, db: Db, finished: bool = True) -> None:
    db.execute("UPDATE FF_AG_RUN SET STATUS = ?, FINISHED_AT = ? WHERE RUN_ID = ?",
               (status, db_ts() if finished else None, run_id))


def _tariff_bands(state: "planning.DisruptionState", shares: dict, ctx: AgentCtx) -> dict[str, SignalBand]:
    """A1 margin re-check (dry run, nothing written) at each formulation's tariff-implied API cost multiplier."""
    groups: dict[float, list[str]] = {}
    for form, mult in planning.tariff_multipliers(state, shares).items():
        groups.setdefault(mult, []).append(form)
    bands: dict[str, SignalBand] = {}
    dry = AgentCtx(ctx.db, ctx.rules, ctx.run.model_copy(update={"dry_run": True}))
    for mult, forms in groups.items():
        for sig in a1.run(forms, dry, api_cost_multiplier=mult):
            bands[sig.formulation_id] = sig.band
    return bands


async def simulate_whatif(specs: Sequence[WhatIfSpec], settings: Settings | None = None, db: Db | None = None,
                          rules: Rules | None = None, save: bool = True) -> ScenarioPlan:
    """Scenario Planning only: simulate the what-ifs and rank strategies. Takes no action."""
    s, rules = settings or load_settings(), rules or load_rules()
    db = db or get_db(s)
    rdb = read_replica(db, s)
    ctx = _dx_ctx(f"wf-{uuid.uuid4().hex[:8]}", rdb, rules, dry_run=True)
    specs = planning.normalise(specs, rdb)
    state = planning.build_state(specs)
    bands = _tariff_bands(state, planning.origin_shares(rdb, rules), ctx) if state.tariffs else {}
    plan = await planning.narrate(planning.run(specs, ctx, margin_bands=bands), s)
    plan = plan.model_copy(update={"run_id": None})
    if save:
        planning.save(plan, db)
    return plan


async def execute_response(specs: Sequence[WhatIfSpec], settings: Settings | None = None, db: Db | None = None,
                           rules: Rules | None = None, signal_id: str | None = None,
                           run_id: str | None = None) -> dict[str, Any]:
    """Plan and execute a disruption response end to end (autonomous within policy, the rest to humans).

    Phase 1: every agent computes against a read replica of the reference/MM tables and writes nothing.
    Phase 2: one transaction writes their outputs, decisions and hash-chained audit rows in stage order.
    """
    s, rules = settings or load_settings(), rules or load_rules()
    db = db or get_db(s)
    rdb = read_replica(db, s)
    run_id = run_id or f"dx-{uuid.uuid4().hex[:8]}"
    ctx = _dx_ctx(run_id, rdb, rules, dry_run=True)
    specs = planning.normalise(specs, rdb)
    if not specs:
        raise ValueError("no what-if specs to respond to")

    # Scenario Planning (A1 margin re-check only when tariffs move API costs)
    state = planning.build_state(specs)
    shares = planning.origin_shares(rdb, rules)
    bands = _tariff_bands(state, shares, ctx) if state.tariffs else {}
    plan = await planning.narrate(planning.run(specs, ctx, margin_bands=bands, signal_id=signal_id), s)
    gov = compliance.Governor(rules, rdb)
    plan_decisions = [compliance.plan_decision(plan, run_id)]
    # Rerouting & Logistics
    lp = logistics.plan(state, plan, ctx)
    shipment_decisions = compliance.review_shipments(lp, gov)
    # Inventory Rebalancing
    ip = inventory.plan(plan, lp, ctx)
    ss = compliance.safety_stock_decision(ip)
    inv_decisions = compliance.review_transfers(ip, gov) + ([ss] if ss else [])
    # Remaining shortfall -> existing A4 optimiser + A5 validator -> Chief Pharmacist gate
    scenarios: list[Scenario] = []
    gate: list[Scenario] = []
    recs: list[Recommendation] = []
    if ip.procurement:
        scenarios = inventory.screen(a4.run(ip.procurement, ctx), plan, state, shares, rules)
        top_n = int(rules.value("disruption", "inventory.max_gate_options_per_formulation"))
        gate = [x for x in scenarios if x.rank is not None and x.rank <= top_n]
        recs = a5.run(gate, ip.procurement, ctx)
    proc_decisions = compliance.procurement_decisions(recs, scenarios, run_id)
    decisions = plan_decisions + shipment_decisions + inv_decisions + proc_decisions
    pending = [d for d in decisions if d.status is DecisionStatus.PENDING]
    status = "AWAITING_DECISIONS" if pending else "EXECUTED"
    summary = {"decisions": len(decisions), "pending": len(pending),
               "auto_executed": sum(d.status is DecisionStatus.EXECUTED for d in decisions),
               "blocked": sum(d.status is DecisionStatus.BLOCKED for d in decisions),
               "auto_spend_inr": round(gov.auto_spent, 2)}

    with db.transaction():
        _dx_run_row(run_id, status, db, rules, forms=[i.formulation_id for i in plan.impacts], specs=specs,
                    finished=True)
        chain = a6.Chain(run_id, db)
        planning.save(plan, db)
        _hop(chain, planning.AGENT, [x.model_dump(mode="json") for x in specs], plan.model_dump(mode="json"),
             plan.data_sources, rules)
        compliance.audit_decisions(plan_decisions, chain, rules.versions)
        logistics.save(lp, db)
        _hop(chain, logistics.AGENT, {"plan_id": plan.plan_id}, lp.model_dump(mode="json"),
             [DataSourceRef(source="FF_MM_EKPO", is_proxy="SYNTH"),
              DataSourceRef(source="rules/sea_routes.yaml", is_proxy="PROXY")], rules)
        compliance.audit_decisions(shipment_decisions, chain, rules.versions)
        inventory.save(ip, db)
        _hop(chain, inventory.AGENT, {"plan_id": plan.plan_id}, ip.model_dump(mode="json"),
             [DataSourceRef(source="FF_V_BATCH_FEFO", is_proxy="SYNTH"),
              DataSourceRef(source="FF_MM_MSEG", is_proxy="SYNTH")], rules)
        compliance.audit_decisions(inv_decisions, chain, rules.versions)
        if ip.procurement:
            if scenarios:
                a4._write(db, scenarios)
            _hop(chain, "A4_RESILIENCE", {"forecasts": len(ip.procurement)},
                 [x.model_dump(mode="json") for x in scenarios],
                 [DataSourceRef(source="FF_REF_PRODUCER", is_proxy="REAL"),
                  DataSourceRef(source="FF_MM_EKPO", is_proxy="SYNTH")], rules)
            if recs:
                a5._write(db, recs, {x.scenario_id: x.formulation_id for x in gate})
            _hop(chain, "A5_VALIDATOR", {"scenarios": len(gate)}, [r.model_dump(mode="json") for r in recs],
                 [DataSourceRef(source="rules/dpco.yaml", is_proxy="REAL"),
                  DataSourceRef(source="rules/sop_controls.yaml", is_proxy="REAL")], rules)
            compliance.audit_decisions(proc_decisions, chain, rules.versions)
        _hop(chain, compliance.AGENT, {"decision_ids": [d.decision_id for d in decisions]}, summary,
             [DataSourceRef(source="rules/disruption.yaml", is_proxy="PROXY")], rules)
        compliance.save_decisions(decisions, db)
        chain.flush()
        if signal_id:
            sensing.set_status(signal_id, "PLANNED", db, run_id)
    valid, err = a6.verify_chain(run_id, db)
    return {
        "run_id": run_id, "status": status, "signal_id": signal_id, "plan": plan.model_dump(mode="json"),
        "logistics": lp.model_dump(mode="json"), "inventory": ip.model_dump(mode="json"),
        "procurement": {"scenarios": [x.model_dump(mode="json") for x in scenarios],
                        "recommendations": [r.model_dump(mode="json") for r in recs]},
        "decisions": [d.model_dump(mode="json") for d in decisions], "summary": summary,
        "audit_chain": {"valid": valid, "error": err},
    }


async def respond(signal: DisruptionSignalIn, settings: Settings | None = None, db: Db | None = None,
                  rules: Rules | None = None) -> dict[str, Any]:
    """Full agentic response to one external signal, starting with Disruption Sensing."""
    s, rules = settings or load_settings(), rules or load_rules()
    db = db or get_db(s)
    rdb = read_replica(db, s)
    run_id = f"dx-{uuid.uuid4().hex[:8]}"
    assessment = await sensing.assess(signal, settings=s, db=rdb, rules=rules)
    base = {"run_id": run_id, "signal_id": signal.signal_id, "assessment": assessment.model_dump(mode="json")}
    triage = [compliance.triage_decision(signal, assessment, run_id)] if assessment.escalated else []
    specs = [] if assessment.escalated or not sensing.is_actionable(assessment, rules) else \
        planning.specs_from_assessment(assessment, signal.raw_text, rules, rdb)
    status = "ESCALATED" if triage else ("PLANNED" if specs else "MONITORING")
    run_status = {"ESCALATED": "AWAITING_TRIAGE", "PLANNED": "RUNNING", "MONITORING": "MONITORING"}[status]

    with db.transaction():
        _dx_run_row(run_id, run_status, db, rules, finished=status == "MONITORING")
        chain = a6.Chain(run_id, db)
        _hop(chain, sensing.AGENT, {"signal_id": signal.signal_id, "source": signal.source},
             assessment.model_dump(mode="json"),
             [DataSourceRef(source=signal.source[:100], source_url=signal.source_url, is_proxy="PROXY"),
              DataSourceRef(source="rules/sea_routes.yaml", is_proxy="PROXY")], rules)
        compliance.audit_decisions(triage, chain, rules.versions)
        compliance.save_decisions(triage, db)
        chain.flush()
        sensing.record(signal, assessment, status, db, run_id)

    if status != "PLANNED":
        return {**base, "status": status, "decisions": [d.model_dump(mode="json") for d in triage]}
    result = await execute_response(specs, s, db, rules, signal_id=signal.signal_id, run_id=run_id)
    return {**base, **result}


async def scan_feed(signals: Sequence[DisruptionSignalIn], settings: Settings | None = None, db: Db | None = None,
                    rules: Rules | None = None) -> list[dict[str, Any]]:
    """Monitoring pass over a batch of feed signals; already-assessed signal ids are skipped."""
    s, rules = settings or load_settings(), rules or load_rules()
    db = db or get_db(s)
    seen = sensing.already_seen([x.signal_id for x in signals], db)
    out = []
    for sig in signals:
        if sig.signal_id in seen:
            out.append({"signal_id": sig.signal_id, "status": "SKIPPED_DUPLICATE"})
            continue
        r = await respond(sig, s, db, rules)
        out.append({k: r.get(k) for k in ("run_id", "signal_id", "status", "summary")}
                   | {"severity": r["assessment"]["severity"], "event_type": r["assessment"]["event_type"]})
    return out


async def resolve_decision(decision_id: str, approver: str, approve: bool, option_id: str | None = None,
                           reason: str | None = None, settings: Settings | None = None, db: Db | None = None,
                           rules: Rules | None = None) -> dict[str, Any]:
    """Apply a human decision: the owning agent executes it and the chain records who decided what."""
    s, rules = settings or load_settings(), rules or load_rules()
    db = db or get_db(s)
    d = compliance.resolve(decision_id, approver, approve, option_id, reason, db, rules)
    effect: dict[str, Any] | None = None
    if d.decision_type is DecisionType.SHIPMENT_ROUTING:
        effect = logistics.apply_decision(d.run_id, d.decision_id, d.chosen_option, approve, db)
    elif d.decision_type is DecisionType.STOCK_TRANSFER:
        effect = inventory.apply_decision(d.decision_id, approve and d.chosen_option == "TRANSFER", db)
    elif d.decision_type is DecisionType.SIGNAL_TRIAGE:
        signal_id = str(d.options[0].detail["signal_id"])
        loaded = sensing.load_recorded(signal_id, db)
        if not approve or d.chosen_option == "DISMISS" or loaded is None:
            sensing.set_status(signal_id, "DISMISSED", db)
            _dx_run_status(d.run_id, "DISMISSED", db)
            effect = {"status": "DISMISSED"}
        else:
            signal, assessment = loaded
            specs = planning.specs_from_assessment(assessment, signal.raw_text, rules, read_replica(db, s),
                                                   force_closure=d.chosen_option == "CONFIRM_CLOSURE")
            if not specs:
                sensing.set_status(signal_id, "MONITORING", db)
                _dx_run_status(d.run_id, "MONITORING", db)
                effect = {"status": "MONITORING", "note": "confirmed, but no mapped lane, port or origin to plan for"}
            else:
                sensing.set_status(signal_id, "PLANNED", db, d.run_id)
                effect = await execute_response(specs, s, db, rules, signal_id=signal_id, run_id=d.run_id)
    open_left = compliance.decisions(db, run_id=d.run_id, status=DecisionStatus.PENDING.value)
    if d.decision_type is not DecisionType.SIGNAL_TRIAGE and not open_left:
        _dx_run_status(d.run_id, "EXECUTED", db)
    return {"decision": d.model_dump(mode="json"), "effect": effect, "pending_in_run": len(open_left)}


# ---------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    from agents.common import load_fixture_db

    p = argparse.ArgumentParser(prog="python -m agents.orchestrator", description="Kairos Pipeline Orchestrator")
    p.add_argument("--molecule", action="append", default=[], help="Target formulation id (repeatable)")
    p.add_argument("--shock", type=float, default=1.0, help="API cost shock multiplier (e.g. 1.3 = +30%)")
    p.add_argument("--fixtures", action="store_true", help="Use in-memory test fixtures")
    p.add_argument("--dry-run", action="store_true", help="Don't write agent outputs or audit records")
    a = p.parse_args(argv)

    db = load_fixture_db() if a.fixtures else get_db()
    with db:
        ctx = AgentCtx(db=db, rules=load_rules(), run=RunContext(run_id="cli-test", dry_run=a.dry_run))
        res = run(
            form_ids=a.molecule,
            shock=a.shock,
            ctx=ctx,
            dry_run=a.dry_run,
        )

    print(f"\n=======================================================")
    print(f"Kairos Pipeline Run Complete: {res['run_id']}")
    print(f"Status: {res['status']} | Shock: x{res['shock']:g}")
    print(f"=======================================================")
    print(f"A1 Risk Signals:       {len(res['signals'])}")
    print(f"A2 Dependency Profiles:{len(res['dependencies'])}")
    print(f"A3 Shortage Forecasts: {len(res['forecasts'])}")
    print(f"A4 Action Scenarios:   {len(res['scenarios'])}")
    print(f"A5 Recommendations:    {len(res['recommendations'])}")

    if res['forecasts']:
        print("\n--- A3 Forecaster Signals & Causes ---")
        for f in res['forecasts']:
            print(f"  [{f.exit_risk_band.value}] {f.formulation_id} -> exposure={f.exposure:.2f}, "
                  f"cover={f.days_of_cover or 0:.0f}d, cause={f.cause_code.value}")

    if res['recommendations']:
        print("\n--- A5 Recommendations Reaching Human Gate ---")
        for r in res['recommendations']:
            print(f"  Scenario {r.scenario_id}: OVERALL={r.overall.value} (second approver={r.needs_second_approver})")
            for c in r.checks:
                if c.status.value != "PASS":
                    print(f"    - {c.check_id} [{c.status.value}]: {c.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
