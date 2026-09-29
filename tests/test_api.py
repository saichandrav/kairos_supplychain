"""Tests for Kairos REST API (Stage S09)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app, get_current_settings
from agents.common import load_fixture_db
from agents.contracts import ApprovalAction, ApprovalChecklist, DraftPR
from agents.ctx import Settings, get_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def default_test_settings() -> Any:
    app.dependency_overrides[get_current_settings] = lambda: Settings(app_api_key="", db_backend="sqlite")
    yield
    app.dependency_overrides.clear()


def test_health_check() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["app"] == "Kairos"


def test_logo_asset() -> None:
    resp = client.get("/logo.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


def test_api_key_auth_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    # Configure an API key
    test_settings = Settings(app_api_key="secret-token-123", db_backend="sqlite")
    app.dependency_overrides[get_current_settings] = lambda: test_settings

    try:
        # Request without header fails with 401
        resp_unauth = client.get("/watchlist")
        assert resp_unauth.status_code == 401

        # Request with wrong key fails with 401
        resp_wrong = client.get("/watchlist", headers={"X-API-Key": "wrong-key"})
        assert resp_wrong.status_code == 401

        # Request with correct key passes
        resp_ok = client.get("/watchlist", headers={"X-API-Key": "secret-token-123"})
        assert resp_ok.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_watchlist_returns_items() -> None:
    resp = client.get("/watchlist")
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)
    if items:
        first = items[0]
        assert "FORM_ID" in first
        assert "GENERIC" in first


def test_molecule_detail_route() -> None:
    resp = client.get("/molecule/BENZYL-PENICILLIN-86CA1B40")
    assert resp.status_code == 200
    data = resp.json()
    assert "formulation" in data
    assert "scenarios" in data
    assert "graph" in data


def test_approval_and_s4_pr_generation() -> None:
    # Setup a scenario & recommendation in the db
    db = get_db()
    with db:
        run_id = "test-api-run"
        scenario_id = f"{run_id}:TEST-MOL:01"
        draft = DraftPR(
            Material="M100010",
            Plant="1000",
            RequestedQuantity=250.0,
            BaseUnit="PAC",
            DeliveryDate=datetime.now(timezone.utc).date(),
            PurchasingGroup="001",
            FixedSupplier="V1001",
            PurchaseRequisitionPrice=15.00,
            PurReqnItemCurrency="INR",
        )
        db.execute(
            "INSERT OR REPLACE INTO FF_AG_RECOMMENDATION (RUN_ID, SCENARIO_ID, FORM_ID, OVERALL, NEEDS_SECOND_APPROVER, DRAFT_PR_JSON, CREATED_AT) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, scenario_id, "TEST-MOL", "READY", 0, draft.model_dump_json(by_alias=True), datetime.now(timezone.utc).isoformat())
        )
        db.execute(
            "INSERT OR REPLACE INTO FF_AG_SCENARIO (RUN_ID, SCENARIO_ID, FORM_ID, OPTION_TYPE, SUPPLIER, MATERIAL, QTY, COST_INR, COVERAGE_DAYS, CREATED_AT) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, scenario_id, "TEST-MOL", "BUFFER", "V1001", "M100010", 250.0, 3750.0, 60.0, datetime.now(timezone.utc).isoformat())
        )

        payload = {
            "scenario_id": scenario_id,
            "decision": "APPROVE",
            "checklist": {
                "cp01_material_master": True,
                "cp02_rol_roq_stock": True,
                "cp04_supplier_verified": True,
                "price_within_ceiling_gst": True,
                "shelf_life_fefo_ok": True,
                "cause_reviewed_not_claim": True,
            },
            "reason": "Routine clinical buffer",
            "user": "chief_pharmacist@psg.in",
            "run_id": run_id,
        }

        resp = client.post("/approval", json=payload)
        assert resp.status_code == 200
        res = resp.json()
        assert res["status"] == "APPROVED_AND_ACTED"
        assert res["action_result"]["banfn"] is not None

        # Verify audit report endpoint serves HTML
        rep_resp = client.get(f"/audit/{scenario_id}/report")
        assert rep_resp.status_code == 200
        assert "NABH Procurement Audit Dossier" in rep_resp.text
