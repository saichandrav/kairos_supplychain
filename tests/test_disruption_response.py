"""Disruption response agents end to end: Scenario Planning, Rerouting & Logistics, Inventory Rebalancing,
Compliance & Audit and the human-in-the-loop queue, on a small deterministic SYNTH hospital (no network)."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agents import a6_audit as a6, compliance, orchestrator, scenario_planning as sp, sea_routes
from agents.common import AgentCtx
from agents.contracts import (
    DecisionStatus, DecisionType, DisruptionSignalIn, RunContext, StrategyType, WhatIfKind, WhatIfSpec,
)
from agents.ctx import Settings, get_db
from agents.rules import Rules, load_rules
from api.main import app, get_current_settings
from ingest import load_hana as lh
from services.openrouter import OpenRouterError

AS_OF = date(2026, 9, 25)
PROV = {"SOURCE": "test", "IS_PROXY": "SYNTH"}
NO_LLM = Settings(db_backend="sqlite", openrouter_api_key="", app_api_key="")
LLM = Settings(db_backend="sqlite", openrouter_api_key="test-key", app_api_key="")


def put(db, table: str, rows: list[dict]) -> None:
    t = lh.schema_tables()[table]
    lh.upsert_rows(db, t, [{**r, **(PROV if t.has_provenance else {})} for r in rows])


def build_hospital(db, amp_cent_stock: int = 1200) -> None:
    """Ampicillin API from Italy (Suez lane) and Japan; Atropine API from China (Malacca lane)."""
    lh.create_objects(db)
    put(db, "FF_CFG_PARAM", [{"NAME": "AS_OF_DATE", "DATE_VALUE": AS_OF.isoformat()}])
    put(db, "FF_REF_FORMULATION", [
        {"FORM_ID": "F-AMP", "GENERIC": "Ampicillin", "THERAPEUTIC_CLASS": "Antibiotic", "NLEM_LEVEL": "P,S,T"},
        {"FORM_ID": "F-SUB", "GENERIC": "Amoxicillin", "THERAPEUTIC_CLASS": "Antibiotic", "NLEM_LEVEL": "P,S,T"},
        {"FORM_ID": "F-ATR", "GENERIC": "Atropine", "THERAPEUTIC_CLASS": "Anticholinergic", "NLEM_LEVEL": "P,S,T"},
    ])
    put(db, "FF_REF_API", [{"API_ID": a, "NAME": a} for a in ("API-AMP", "API-SUB", "API-ATR")])
    put(db, "FF_REF_FORM_API", [{"FORM_ID": f, "API_ID": a} for f, a in
                                (("F-AMP", "API-AMP"), ("F-SUB", "API-SUB"), ("F-ATR", "API-ATR"))])
    put(db, "FF_REF_API_ORIGIN", [
        {"API_ID": "API-AMP", "MONTH": "2026-09-01", "COUNTRY": "IT", "SHARE": 0.8},
        {"API_ID": "API-AMP", "MONTH": "2026-09-01", "COUNTRY": "JP", "SHARE": 0.2},
        {"API_ID": "API-SUB", "MONTH": "2026-09-01", "COUNTRY": "JP", "SHARE": 1.0},
        {"API_ID": "API-ATR", "MONTH": "2026-09-01", "COUNTRY": "CN", "SHARE": 0.9},
        {"API_ID": "API-ATR", "MONTH": "2026-09-01", "COUNTRY": "KR", "SHARE": 0.1},
    ])
    put(db, "FF_REF_MANUFACTURER", [{"ID": m, "NAME": m} for m in ("MFR-1", "MFR-2", "MFR-3")])
    put(db, "FF_REF_PRODUCER", [
        {"FORM_ID": "F-AMP", "MANUFACTURER_ID": "MFR-1", "MARKET_SHARE": 0.6},
        {"FORM_ID": "F-AMP", "MANUFACTURER_ID": "MFR-2", "MARKET_SHARE": 0.4},
        {"FORM_ID": "F-SUB", "MANUFACTURER_ID": "MFR-3", "MARKET_SHARE": 1.0},
        {"FORM_ID": "F-ATR", "MANUFACTURER_ID": "MFR-1", "MARKET_SHARE": 1.0},
    ])
    put(db, "FF_REF_CEILING_PRICE", [
        {"FORM_ID": f, "EFFECTIVE_FROM": "2025-04-01", "SO_NUMBER": f"SO-{f}", "CEILING_PRICE": p, "GST_RATE": 0.05}
        for f, p in (("F-AMP", "25.0"), ("F-SUB", "20.0"), ("F-ATR", "12.0"))])
    put(db, "FF_MM_T001W", [{"WERKS": "H001", "NAME1": "Demo Hospital"}])
    put(db, "FF_MM_MARA", [
        {"MATNR": "M-AMP", "MAKTX": "AMPICILLIN 500 MG INJ", "FORM_ID": "F-AMP", "VED": "V", "RAUBE": "AMBIENT"},
        {"MATNR": "M-SUB", "MAKTX": "AMOXICILLIN 500 MG CAP", "FORM_ID": "F-SUB", "VED": "E", "RAUBE": "AMBIENT"},
        {"MATNR": "M-ATR", "MAKTX": "ATROPINE 0.6 MG INJ", "FORM_ID": "F-ATR", "VED": "V", "RAUBE": "AMBIENT"},
    ])
    put(db, "FF_MM_LFA1", [
        {"LIFNR": "V1", "NAME1": "Alpha Pharma Distributors", "DRUG_LICENCE_NO": "DL-1", "GSTIN": "33AAAAA0000A1Z5"},
        {"LIFNR": "V2", "NAME1": "Kaveri Medical Agencies", "DRUG_LICENCE_NO": "DL-2", "GSTIN": "33BBBBB0000B1Z5"},
        {"LIFNR": "V3", "NAME1": "Blocked Traders", "SPERR": "X"},
    ])
    put(db, "FF_MM_MCHB", [
        {"MATNR": "M-AMP", "WERKS": "H001", "LGORT": "CENT", "CHARG": "A1", "CLABS": amp_cent_stock, "VFDAT": "2028-06-30"},
        {"MATNR": "M-AMP", "WERKS": "H001", "LGORT": "ABLK", "CHARG": "A2", "CLABS": 20, "VFDAT": "2028-06-30"},
        {"MATNR": "M-SUB", "WERKS": "H001", "LGORT": "CENT", "CHARG": "S1", "CLABS": 6000, "VFDAT": "2028-06-30"},
        {"MATNR": "M-ATR", "WERKS": "H001", "LGORT": "CENT", "CHARG": "T1", "CLABS": 3000, "VFDAT": "2028-06-30"},
    ])
    mseg = []
    for i in range(90):
        day = (AS_OF - timedelta(days=i)).isoformat()
        for mat, lgort, qty in (("M-AMP", "ABLK", 30), ("M-AMP", "CENT", 10), ("M-SUB", "CENT", 50), ("M-ATR", "CENT", 20)):
            mseg.append({"MBLNR": f"D{i:03d}{mat[-3:]}{lgort[0]}", "MJAHR": 2026, "ZEILE": 1, "BWART": "261", "MATNR": mat,
                         "WERKS": "H001", "LGORT": lgort, "MENGE": qty + (i % 3), "BUDAT": day})
    mseg.append({"MBLNR": "GR0001", "MJAHR": 2026, "ZEILE": 1, "BWART": "101", "MATNR": "M-AMP", "WERKS": "H001",
                 "LGORT": "CENT", "MENGE": 1000, "BUDAT": "2026-06-10", "EBELN": "4500000000", "EBELP": 10})
    put(db, "FF_MM_MSEG", mseg)
    put(db, "FF_MM_EKKO", [
        {"EBELN": "4500000000", "LIFNR": "V2", "BEDAT": "2026-06-01"},
        {"EBELN": "4500000001", "LIFNR": "V1", "BEDAT": "2026-09-20"},
    ])
    put(db, "FF_MM_EKPO", [
        {"EBELN": "4500000000", "EBELP": 10, "MATNR": "M-AMP", "WERKS": "H001", "MENGE": 1000, "NETPR": 17.0,
         "PEINH": 1, "EINDT": "2026-06-10"},
        {"EBELN": "4500000001", "EBELP": 10, "MATNR": "M-AMP", "WERKS": "H001", "MENGE": 600, "NETPR": 18.0,
         "PEINH": 1, "EINDT": "2026-10-05"},
    ])


@pytest.fixture
def db(tmp_path):
    d = get_db(Settings(db_backend="sqlite"), sqlite_path=tmp_path / "dx.sqlite")
    build_hospital(d)
    yield d
    d.close()


@pytest.fixture
def urgent_db(tmp_path):
    """Ampicillin cover ~15 days: a Cape reroute would land after stock-out."""
    d = get_db(Settings(db_backend="sqlite"), sqlite_path=tmp_path / "dx_urgent.sqlite")
    build_hospital(d, amp_cent_stock=580)
    yield d
    d.close()


@pytest.fixture(scope="module")
def rules() -> Rules:
    return load_rules()


def ctx(db, rules) -> AgentCtx:
    return AgentCtx(db, rules, RunContext(run_id="t-run", as_of=AS_OF, dry_run=True))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- sea lanes + what-if parsing


def test_place_matching_is_whole_phrase():
    assert not sea_routes.match_places("a vessel sank in the strait near the sea port")
    assert sea_routes.match_places("attacks in the Red Sea").chokepoints == {"Bab el-Mandeb"}
    assert sea_routes.match_places("Chinese export curbs").countries == {"CN"}
    assert sea_routes.match_places("Nhava Sheva berth delays").indian_ports == {"JNPT/Nhava Sheva"}


def test_malacca_closure_offers_lombok_bypass_and_hold():
    k = sea_routes.LaneConsts(3, 5, 7, 4)
    opts = sea_routes.lane_options("CN", sea_routes.LaneState(blocked_chokepoints={"Strait of Malacca"}), k)
    assert sea_routes.absorb_option(opts).kind == "HOLD"
    best = sea_routes.best_reroute(opts)
    assert best.extra_days == 4 and "Lombok" in best.label


def test_rules_parser_handles_theme_questions(db, rules):
    suez = sp.parse_whatif_rules("What if the Suez route closes?", db, rules)
    assert [(s.kind, s.target) for s in suez] == [(WhatIfKind.CHOKEPOINT_CLOSURE, "Suez Canal")]
    tariff = sp.parse_whatif_rules("What if tariffs increase to 50%?", db, rules)
    assert [(s.kind, s.target, s.magnitude_pct) for s in tariff] == [(WhatIfKind.TARIFF_INCREASE, "ALL", 50.0)]
    offline = sp.parse_whatif_rules("What if a key supplier goes offline for 3 weeks?", db, rules)
    assert offline[0].kind is WhatIfKind.SUPPLIER_OFFLINE and offline[0].duration_days == 21
    assert offline[0].target in ("V1", "V2")
    named = sp.parse_whatif_rules("What if Kaveri Medical goes offline?", db, rules)
    assert named[0].target == "V2"


def test_llm_whatif_parse_is_validated(db, rules):
    llm = {"choices": [{"message": {"content": json.dumps({"specs": [
        {"kind": "CHOKEPOINT_CLOSURE", "target": "Suez", "magnitude_pct": 0, "duration_days": 20},
        {"kind": "NOT_A_KIND", "target": "Mars"}]})}}]}
    with patch("agents.scenario_planning.chat_completion", new_callable=AsyncMock, return_value=llm):
        specs, used_llm, note = run(sp.parse_whatif("Suez shut for 20 days", LLM, db, rules))
    assert not used_llm and "LLM parse failed" in note  # one invalid spec -> whole LLM parse rejected
    assert specs[0].target == "Suez Canal"
    ok = {"choices": [{"message": {"content": json.dumps({"specs": [
        {"kind": "CHOKEPOINT_CLOSURE", "target": "Suez", "magnitude_pct": 0, "duration_days": 20}]})}}]}
    with patch("agents.scenario_planning.chat_completion", new_callable=AsyncMock, return_value=ok):
        specs, used_llm, _ = run(sp.parse_whatif("Suez shut for 20 days", LLM, db, rules))
    assert used_llm and specs == [WhatIfSpec(kind=WhatIfKind.CHOKEPOINT_CLOSURE, target="Suez Canal", duration_days=20)]


def test_unknown_target_rejected():
    with pytest.raises(ValueError):
        sp.resolve_target(WhatIfKind.CHOKEPOINT_CLOSURE, "Atlantis Strait")


# ---------------------------------------------------------------- Scenario Planning


def test_suez_closure_plan_exposes_only_suez_lane(db, rules):
    plan = sp.run([WhatIfSpec(kind=WhatIfKind.CHOKEPOINT_CLOSURE, target="Suez Canal")], ctx(db, rules))
    assert {i.formulation_id for i in plan.impacts} == {"F-AMP"}
    amp = plan.impacts[0]
    assert amp.lanes[0].origin == "IT" and amp.lanes[0].reroute_delay_days == 16
    by = {e.strategy: e for e in amp.strategies}
    assert by[StrategyType.REROUTE].feasible
    assert by[StrategyType.REROUTE].stockout_units < by[StrategyType.ABSORB].stockout_units
    assert plan.recommended_option and plan.options[-1].option_id == "RECOMMENDED_MIX"
    assert "Suez Canal" in sp.template_narrative(plan)


def test_security_risk_is_penalised_not_ignored(db, rules):
    plan = sp.run([WhatIfSpec(kind=WhatIfKind.CHOKEPOINT_RISK, target="Bab el-Mandeb")], ctx(db, rules))
    by = {e.strategy: e for e in plan.impacts[0].strategies}
    assert by[StrategyType.ABSORB].residual_risk > 0  # no stock-out, but a security-risk route
    assert plan.impacts[0].lanes[0].security_risk


def test_supplier_offline_and_tariff_plans(db, rules):
    off = sp.run([WhatIfSpec(kind=WhatIfKind.SUPPLIER_OFFLINE, target="V1")], ctx(db, rules))
    amp = next(i for i in off.impacts if i.formulation_id == "F-AMP")
    assert amp.supply_loss_share > 0 and not amp.lanes
    tar = sp.run([WhatIfSpec(kind=WhatIfKind.TARIFF_INCREASE, target="CN", magnitude_pct=50)], ctx(db, rules))
    assert [i.formulation_id for i in tar.impacts] == ["F-ATR"]
    assert tar.impacts[0].tariff_uplift_pct == pytest.approx(45.0)


def test_narrative_llm_output_must_be_grounded(db, rules):
    plan = sp.run([WhatIfSpec(kind=WhatIfKind.CHOKEPOINT_CLOSURE, target="Suez Canal")], ctx(db, rules))
    bad = {"choices": [{"message": {"content": "Suez closure exposes 999 medicines."}}]}
    with patch("agents.scenario_planning.chat_completion", new_callable=AsyncMock, return_value=bad):
        out = run(sp.narrate(plan, LLM))
    assert not out.narrative_used_llm and out.narrative == sp.template_narrative(plan)
    good = {"choices": [{"message": {"content": "The Suez closure exposes 1 medicine; the recommended mix is preferred."}}]}
    with patch("agents.scenario_planning.chat_completion", new_callable=AsyncMock, return_value=good):
        out = run(sp.narrate(plan, LLM))
    assert out.narrative_used_llm


# ---------------------------------------------------------------- end-to-end response


def test_suez_closure_response_end_to_end(db, rules):
    res = run(orchestrator.execute_response([WhatIfSpec(kind=WhatIfKind.CHOKEPOINT_CLOSURE, target="Suez Canal")],
                                            NO_LLM, db, rules))
    run_id = res["run_id"]
    assert res["audit_chain"] == {"valid": True, "error": None}
    ship = res["logistics"]["shipments"]
    assert [s["ebeln"] for s in ship] == ["4500000001"]
    assert ship[0]["chosen"]["new_eta"] > ship[0]["old_eta"] and ship[0]["status"] == "EXECUTED"
    assert any(t["to_location"] == "ABLK" and t["status"] == "EXECUTED" for t in res["inventory"]["transfers"])
    ss = res["inventory"]["safety_stock"][0]
    assert ss["safety_stock_new"] > ss["safety_stock_old"]
    # procurement: alternates importing the same API are rejected as correlated; the rest go to the pharmacist gate
    alt = [s for s in res["procurement"]["scenarios"] if s["option_type"] == "ALT_SUPPLIER"]
    assert alt and all(s["rank"] is None and "same API" in s["correlated_risk_reason"] for s in alt)
    pending = compliance.decisions(db, run_id=run_id, status="PENDING")
    assert pending and all(d.decision_type is DecisionType.PROCUREMENT_PROPOSAL for d in pending)
    assert not db.query("SELECT * FROM FF_AG_RECOMMENDATION WHERE RUN_ID = ?", (run_id,)).empty
    assert db.query("SELECT STATUS FROM FF_AG_RUN WHERE RUN_ID = ?", (run_id,)).iat[0, 0] == "AWAITING_DECISIONS"
    # every decision is chain-audited; tampering is detected
    db.execute("UPDATE FF_AG_AUDIT_LOG SET OUTPUT_HASH = 'x' WHERE RUN_ID = ? AND SEQ = 1", (run_id,))
    assert a6.verify_chain(run_id, db)[0] is False


def test_risky_route_needs_human_and_alternative_can_be_chosen(urgent_db, rules):
    db = urgent_db
    res = run(orchestrator.execute_response([WhatIfSpec(kind=WhatIfKind.CHOKEPOINT_RISK, target="Bab el-Mandeb")],
                                            NO_LLM, db, rules))
    ship = res["logistics"]["shipments"][0]
    assert ship["chosen"]["security_risk"] and ship["status"] == "PENDING"
    d = compliance.load(ship["decision_id"], db)
    assert any(c.check_id == "SECURITY_RISK" and c.status.value == "WARN" for c in d.checks)
    notice = db.query("SELECT STATUS FROM FF_AG_NOTIFICATION WHERE DECISION_ID = ?", (d.decision_id,))
    assert notice.iat[0, 0] == "HELD"
    with pytest.raises(PermissionError):
        run(orchestrator.resolve_decision(d.decision_id, "agent:dx3", True, None, None, NO_LLM, db, rules))
    alt = next(o.option_id for o in d.options[1:] if not o.detail["security_risk"])
    out = run(orchestrator.resolve_decision(d.decision_id, "chief.pharmacist@psg.in", True, alt, "avoid Red Sea",
                                            NO_LLM, db, rules))
    assert out["effect"]["status"] == "EXECUTED" and out["effect"]["chosen"]["option_id"] == alt
    row = db.query("SELECT STATUS, SECURITY_RISK FROM FF_AG_SHIPMENT_PLAN WHERE DECISION_ID = ?", (d.decision_id,))
    assert (row.iat[0, 0], int(row.iat[0, 1])) == ("EXECUTED", 0)
    assert db.query("SELECT STATUS FROM FF_AG_NOTIFICATION WHERE DECISION_ID = ?", (d.decision_id,)).iat[0, 0] == "QUEUED"
    with pytest.raises(PermissionError, match="not PENDING"):
        run(orchestrator.resolve_decision(d.decision_id, "someone@psg.in", True, None, None, NO_LLM, db, rules))
    assert a6.verify_chain(res["run_id"], db)[0]


def test_cost_threshold_gates_autonomy(db, rules):
    docs = copy.deepcopy(rules.docs)
    docs["disruption"]["governance"]["human_cost_threshold_inr"]["value"] = 1
    strict = Rules(docs)
    res = run(orchestrator.execute_response([WhatIfSpec(kind=WhatIfKind.CHOKEPOINT_CLOSURE, target="Suez Canal")],
                                            NO_LLM, db, strict))
    d = compliance.load(res["logistics"]["shipments"][0]["decision_id"], db)
    assert d.status is DecisionStatus.PENDING and any(c.check_id == "COST_THRESHOLD" for c in d.checks
                                                      if c.status.value == "WARN")


def test_procurement_is_never_resolved_through_the_queue(db, rules):
    res = run(orchestrator.execute_response([WhatIfSpec(kind=WhatIfKind.CHOKEPOINT_CLOSURE, target="Suez Canal")],
                                            NO_LLM, db, rules))
    pr = next(d for d in compliance.decisions(db, run_id=res["run_id"], status="PENDING"))
    with pytest.raises(PermissionError, match="Chief Pharmacist gate"):
        compliance.resolve(pr.decision_id, "pharmacist@psg.in", True, None, None, db, rules)


def test_ambiguous_signal_escalates_then_analyst_confirms(db, rules):
    sig = DisruptionSignalIn(signal_id="S-1", source="test_feed",
                             raw_text="Reports of an incident near the Suez Canal; details unconfirmed.")
    res = run(orchestrator.respond(sig, NO_LLM, db, rules))
    assert res["status"] == "ESCALATED" and res["assessment"]["escalated"]
    tri = res["decisions"][0]["decision_id"]
    out = run(orchestrator.resolve_decision(tri, "analyst@psg.in", True, "CONFIRM_CLOSURE", "confirmed", NO_LLM, db, rules))
    assert out["effect"]["plan"]["specs"][0]["kind"] == "CHOKEPOINT_CLOSURE"
    assert db.query("SELECT STATUS FROM FF_AG_DISRUPTION WHERE SIGNAL_ID = 'S-1'").iat[0, 0] == "PLANNED"
    agents = db.query("SELECT AGENT FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ? ORDER BY SEQ", (res["run_id"],))["AGENT"].tolist()
    assert agents[:3] == ["DX1_SENSING", "DX1_SENSING", "HITL_DECISION"] and "DX5_COMPLIANCE" in agents
    assert a6.verify_chain(res["run_id"], db)[0]


def test_dismissed_signal_stops(db, rules):
    sig = DisruptionSignalIn(signal_id="S-2", source="test_feed", raw_text="Heavy rain postpones a football match.")
    res = run(orchestrator.respond(sig, NO_LLM, db, rules))
    assert res["status"] == "ESCALATED"
    with pytest.raises(ValueError, match="reason"):
        run(orchestrator.resolve_decision(res["decisions"][0]["decision_id"], "analyst@psg.in", False, None, None,
                                          NO_LLM, db, rules))
    out = run(orchestrator.resolve_decision(res["decisions"][0]["decision_id"], "analyst@psg.in", False, None,
                                            "not a disruption", NO_LLM, db, rules))
    assert out["effect"] == {"status": "DISMISSED"}


def test_confident_llm_signal_is_planned_autonomously(db, rules):
    llm = {"choices": [{"message": {"content": json.dumps({
        "event_type": "MARITIME_SECURITY", "summary": "Attack near Bab el-Mandeb", "severity": "HIGH",
        "location": "Bab el-Mandeb", "confidence": 0.9, "requires_human_review": True})}}]}
    sig = DisruptionSignalIn(signal_id="S-3", source="test_feed", raw_text="Vessel attacked near Bab el-Mandeb.")
    with patch("agents.disruption_sensing.chat_completion", new_callable=AsyncMock, return_value=llm), \
         patch("agents.scenario_planning.chat_completion", new_callable=AsyncMock, side_effect=OpenRouterError("down")):
        res = run(orchestrator.respond(sig, LLM, db, rules))
    assert res["status"] in ("EXECUTED", "AWAITING_DECISIONS")
    assert res["plan"]["specs"] == [{"kind": "CHOKEPOINT_RISK", "target": "Bab el-Mandeb", "magnitude_pct": 0.0,
                                     "duration_days": 30, "label": None}]


def test_report_html(db, rules):
    res = run(orchestrator.execute_response([WhatIfSpec(kind=WhatIfKind.CHOKEPOINT_CLOSURE, target="Suez Canal")],
                                            NO_LLM, db, rules))
    rep = compliance.report(res["run_id"], db)
    assert rep["audit_chain"]["valid"] and rep["decision_counts"]
    html = compliance.report_html(rep, [], [], [])
    assert "Disruption Response Audit Dossier" in html and "VERIFIED" in html


# ---------------------------------------------------------------- API


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "api.sqlite"
    d = get_db(Settings(db_backend="sqlite"), sqlite_path=path)
    build_hospital(d)
    d.close()
    monkeypatch.setattr("agents.ctx.SQLITE_PATH", path)
    app.dependency_overrides[get_current_settings] = lambda: NO_LLM
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_whatif_execute_decide_and_report(client):
    r = client.post("/disruption/whatif", json={"question": "What if the Suez route closes?"})
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]
    assert plan["specs"][0]["target"] == "Suez Canal" and plan["impacts"]
    assert client.get("/disruption/whatif").json()[0]["PLAN_ID"] == plan["plan_id"]

    r = client.post(f"/disruption/whatif/{plan['plan_id']}/execute")
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    pending = client.get("/disruption/decisions", params={"status": "PENDING"}).json()
    assert pending
    bad = client.post(f"/disruption/decisions/{pending[0]['decision_id']}/resolve",
                      json={"approver": "system-bot", "approve": True})
    assert bad.status_code == 403
    detail = client.get(f"/disruption/runs/{run_id}").json()
    assert detail["audit_chain"]["valid"] and detail["shipments"] and detail["safety_stock"]
    assert any(x["RUN_ID"] == run_id for x in client.get("/disruption/runs").json())
    html = client.get(f"/disruption/runs/{run_id}/report")
    assert html.status_code == 200 and "Disruption Response Audit Dossier" in html.text


def test_api_whatif_rejects_unrecognised(client):
    r = client.post("/disruption/whatif", json={"question": "What if it rains on Mars?"})
    assert r.status_code == 422


def test_api_feed_scan_skips_duplicates(client):
    first = client.post("/disruption/feed/scan").json()["results"]
    assert len(first) == 6 and all(x["status"] == "ESCALATED" for x in first)  # no LLM key -> analyst triage
    again = client.post("/disruption/feed/scan").json()["results"]
    assert {x["status"] for x in again} == {"SKIPPED_DUPLICATE"}
    assert len(client.get("/disruption/signals").json()) == 6
