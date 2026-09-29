"""Tests for A6 Audit, Hash Chain integrity, and Human Gate Action (Stage S08)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agents import a6_audit as a6
from agents.common import load_fixture_db
from agents.contracts import (
    ApprovalAction, ApprovalChecklist, ApprovalDecision, AuditEvent,
    CheckResult, CheckStatus, DataSourceRef, DraftPR, OptionType,
    Overall, Recommendation, Scenario,
)


def _complete_checklist() -> ApprovalChecklist:
    return ApprovalChecklist(
        cp01_material_master=True,
        cp02_rol_roq_stock=True,
        cp04_supplier_verified=True,
        price_within_ceiling_gst=True,
        shelf_life_fefo_ok=True,
        cause_reviewed_not_claim=True,
    )


def test_audit_chain_sequential_integrity() -> None:
    db = load_fixture_db()
    run_id = "test-run-001"

    # 1. Append 3 hops
    e1 = AuditEvent(
        run_id=run_id, seq=0, agent="A1",
        input_hash=a6.hash_data({"in": 1}), output_hash=a6.hash_data({"out": 1})
    )
    a6.audit(e1, db)

    e2 = AuditEvent(
        run_id=run_id, seq=1, agent="A2",
        input_hash=a6.hash_data({"in": 2}), output_hash=a6.hash_data({"out": 2})
    )
    a6.audit(e2, db)

    e3 = AuditEvent(
        run_id=run_id, seq=2, agent="A3",
        input_hash=a6.hash_data({"in": 3}), output_hash=a6.hash_data({"out": 3})
    )
    a6.audit(e3, db)

    # 2. Verify clean chain
    valid, err = a6.verify_chain(run_id, db)
    assert valid is True
    assert err is None


def test_tamper_detection_in_audit_chain() -> None:
    db = load_fixture_db()
    run_id = "test-run-tamper"

    e1 = AuditEvent(run_id=run_id, seq=0, agent="A1", input_hash="h1", output_hash="h2")
    a6.audit(e1, db)

    e2 = AuditEvent(run_id=run_id, seq=1, agent="A2", input_hash="h3", output_hash="h4")
    a6.audit(e2, db)

    valid, _ = a6.verify_chain(run_id, db)
    assert valid is True

    # Tamper with row 1 output hash directly in the database
    db.execute(
        "UPDATE FF_AG_AUDIT_LOG SET OUTPUT_HASH = 'tampered_hash' WHERE RUN_ID = ? AND SEQ = 0",
        (run_id,)
    )

    valid, err = a6.verify_chain(run_id, db)
    assert valid is False
    assert "Hash mismatch at seq=0" in str(err)


def test_act_refused_without_approval() -> None:
    db = load_fixture_db()
    scenario_id = "scen-unapproved"

    with pytest.raises(PermissionError, match="No human approval found"):
        a6.act(scenario_id, db)


def test_rejection_requires_reason() -> None:
    chk = _complete_checklist()
    with pytest.raises(ValueError, match="REJECT requires a reason"):
        ApprovalDecision(
            run_id="run-rej",
            scenario_id="scen-rej",
            approver="pharmacist@psg.in",
            action=ApprovalAction.REJECT,
            checklist=chk,
            reason="",
        )


def test_approval_creates_s4_pr_in_eban() -> None:
    db = load_fixture_db()
    run_id = "run-approval-ok"
    scenario_id = "scen-app-01"

    # Seed a scenario and recommendation
    draft_pr = DraftPR(
        Material="M100010",
        Plant="1000",
        RequestedQuantity=500.0,
        BaseUnit="PAC",
        DeliveryDate=datetime.now(timezone.utc).date(),
        PurchasingGroup="001",
        FixedSupplier="V1001",
        PurchaseRequisitionPrice=12.50,
        PurReqnItemCurrency="INR",
    )
    db.execute(
        "INSERT INTO FF_AG_RECOMMENDATION (RUN_ID, SCENARIO_ID, FORM_ID, OVERALL, NEEDS_SECOND_APPROVER, DRAFT_PR_JSON, CREATED_AT) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, scenario_id, "BENZYL-PENICILLIN", "READY", 0, draft_pr.model_dump_json(by_alias=True), datetime.now(timezone.utc).isoformat())
    )

    # Approve with complete checklist
    from agents.orchestrator import decide
    res = decide(
        scenario_id=scenario_id,
        user="chief_pharmacist@psg.edu",
        action=ApprovalAction.APPROVE,
        checklist=_complete_checklist(),
        run_id=run_id,
        reason="Approved buffer stock proactively",
        db=db,
    )

    assert res["status"] == "APPROVED_AND_ACTED"
    act_res = res["action_result"]
    assert act_res is not None
    assert act_res["status"] == "POSTED"
    assert act_res["banfn"] is not None

    # Check EBAN row created
    df = db.query("SELECT * FROM FF_MM_EBAN WHERE SCENARIO_ID = ?", (scenario_id,))
    assert not df.empty
    assert df.iloc[0]["BANFN"] == act_res["banfn"]
    assert float(df.iloc[0]["MENGE"]) == 500.0
    assert df.iloc[0]["STATUS"] == "APPROVED"
