from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agents.contracts import (
    ApprovalAction, ApprovalChecklist, ApprovalDecision, AuditEvent, CauseCode, CheckResult,
    CheckStatus, DataQuality, DataSourceRef, DataTag, DependencyProfile, DraftPR, Forecast,
    OptionType, Overall, Recommendation, RiskSignal, RunContext, Scenario, SignalBand,
)
from agents.ctx import Settings, get_db, load_settings


def test_enums():
    assert {c.value for c in CauseCode} == {
        "CEILING_BELOW_COST", "HEADROOM_ERODING", "SINGLE_ORIGIN_API",
        "FEW_PRODUCERS", "QUALITY_NSQ", "SUPPLIER_OTD_DROP", "LOGISTICS_DELAY",
    }
    assert [b.value for b in SignalBand] == ["GREEN", "AMBER", "RED"]
    assert {t.value for t in DataTag} == {"REAL", "PROXY", "SYNTH"}
    assert {o.value for o in OptionType} == {"BUFFER", "ALT_SUPPLIER", "THERAPEUTIC_ALT", "REBALANCE"}


def test_risk_signal_roundtrip():
    s = RiskSignal(
        run_id="r1", formulation_id="F001", band="AMBER", headroom_pct=0.12, headroom_trend=-0.01,
        months_to_breach=12, realisation_inr="1.7084", unit_cost_inr=Decimal("1.5"),
        data_quality=DataQuality(confidence=0.4, tags={"ceiling": DataTag.REAL}),
    )
    assert s.band is SignalBand.AMBER and s.realisation_inr == Decimal("1.7084")
    assert RiskSignal.model_validate_json(s.model_dump_json()) == s


def test_money_precision_and_extra_fields():
    base = dict(run_id="r", formulation_id="F", band="RED", headroom_pct=0, headroom_trend=0,
                data_quality={"confidence": 1})
    with pytest.raises(ValidationError):
        RiskSignal(**base, realisation_inr="1.23456")
    with pytest.raises(ValidationError):
        RiskSignal(**base, surprise=1)


def test_dependency_bounds():
    DependencyProfile(run_id="r", formulation_id="F", n_producers_min=3, hhi=0.4, top_origin_share=0.8)
    with pytest.raises(ValidationError):
        DependencyProfile(run_id="r", formulation_id="F", n_producers_min=3, hhi=1.4)


def test_forecast_window_order():
    f = Forecast(run_id="r", formulation_id="F", exit_risk_band="RED", cause_code="HEADROOM_ERODING",
                 window_months_lo=3, window_months_hi=6, cause_facts={"headroom_pct": 0.04})
    assert f.cause_code is CauseCode.HEADROOM_ERODING
    with pytest.raises(ValidationError):
        Forecast(run_id="r", formulation_id="F", exit_risk_band="RED", cause_code="FEW_PRODUCERS",
                 window_months_lo=6, window_months_hi=3)


def _scenario():
    return Scenario(scenario_id="s1", run_id="r", formulation_id="F001", option_type="ALT_SUPPLIER",
                    supplier="M02", qty=5000, unit_rate_inr="2.1000", cost="10500", coverage_days=60)


def test_scenario_and_recommendation():
    sc = _scenario()
    assert sc.option_type is OptionType.ALT_SUPPLIER and not sc.correlated_risk_flag
    pr = DraftPR(material="MAT-0001", plant="H001", quantity=5000, delivery_date=date(2026, 10, 15),
                 purchasing_group="P01", fixed_supplier="M02", unit_price_inr="2.1000")
    payload = pr.to_s4_payload()
    assert payload["Material"] == "MAT-0001" and payload["RequestedQuantity"] == 5000
    assert DraftPR.model_validate(payload) == pr
    ok = [CheckResult(check_id="PRICE_COMPLIANCE", name="Price", status="PASS")]
    rec = Recommendation(run_id="r", scenario_id=sc.scenario_id, checks=ok, overall="READY", draft_pr=pr)
    assert rec.reaches_gate
    bad = [CheckResult(check_id="PRICE_COMPLIANCE", name="Price", status=CheckStatus.FAIL)]
    with pytest.raises(ValidationError):
        Recommendation(run_id="r", scenario_id="s1", checks=bad, overall="READY")
    assert not Recommendation(run_id="r", scenario_id="s1", checks=bad, overall=Overall.BLOCKED).reaches_gate


def _all_ticked():
    return ApprovalChecklist(**{k: True for k in ApprovalChecklist.model_fields})


def test_approval_needs_full_checklist():
    with pytest.raises(ValidationError, match="checklist"):
        ApprovalDecision(run_id="r", scenario_id="s1", approver="chief.pharm", action="APPROVE")
    d = ApprovalDecision(run_id="r", scenario_id="s1", approver="chief.pharm", action="APPROVE",
                         checklist=_all_ticked())
    assert d.approves and d.checklist.complete
    assert len(ApprovalChecklist.model_fields) == 6


def test_approval_action_rules():
    kw = dict(run_id="r", scenario_id="s1", approver="u")
    with pytest.raises(ValidationError, match="reason"):
        ApprovalDecision(**kw, action="REJECT")
    assert not ApprovalDecision(**kw, action="REJECT", reason="price above ceiling").approves
    with pytest.raises(ValidationError):
        ApprovalDecision(**kw, action="SNOOZE")
    ApprovalDecision(**kw, action=ApprovalAction.SNOOZE, snooze_days=7)
    with pytest.raises(ValidationError):
        ApprovalDecision(**kw, action="EDIT_APPROVE", checklist=_all_ticked())
    ApprovalDecision(**kw, action="EDIT_APPROVE", checklist=_all_ticked(), edited_qty=4000, level=2)


def test_audit_event_and_run_context():
    ev = AuditEvent(run_id="r", seq=0, agent="A1", input_hash="a" * 64, output_hash="b" * 64,
                    data_sources=[DataSourceRef(source="NPPA", is_proxy="REAL")])
    assert ev.prev_hash is None
    ctx = RunContext(run_id="r", trigger="SHOCK", shock={"api_cost_pct": 30})
    assert ctx.shock.api_cost_pct == 30 and ctx.started_at.tzinfo is not None


def test_settings_hide_secrets(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("HANA_PASSWORD", "s3cret-value")
    s = load_settings(dotenv=False)
    assert s.hana_password == "s3cret-value" and "s3cret-value" not in repr(s)
    assert s.hana_port == 443


def test_bad_backend(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "oracle")
    with pytest.raises(ValueError):
        load_settings(dotenv=False)


def test_sqlite_db_roundtrip(tmp_path):
    with get_db(Settings(db_backend="sqlite"), sqlite_path=tmp_path / "t.sqlite") as db:
        db.execute("CREATE TABLE FF_T (ID TEXT, V REAL)")
        assert db.executemany("INSERT INTO FF_T VALUES (?, ?)", [("a", 1.0), ("b", 2.5)]) == 2
        df = db.query("SELECT ID, V FROM FF_T WHERE V > ?", (1.5,))
        assert list(df.columns) == ["ID", "V"] and df.iloc[0]["ID"] == "b"


def test_fixtures_are_synth(fixture_db):
    df = fixture_db.query('SELECT COUNT(*) AS N FROM "FF_FX_FORMULATIONS"')
    assert int(df.iloc[0]["N"]) == 3
    for t in ("FORMULATIONS", "APIS", "PRODUCERS", "API_COST_MONTHLY", "MARA", "MCHB", "MSEG", "BOM_ASSUMPTION"):
        tags = fixture_db.query(f'SELECT DISTINCT IS_PROXY FROM "FF_FX_{t}"')["IS_PROXY"].tolist()
        assert tags == ["SYNTH"], t
    assert int(fixture_db.query('SELECT COUNT(DISTINCT MANUFACTURER_ID) N FROM "FF_FX_PRODUCERS"').iloc[0]["N"]) == 4
    assert int(fixture_db.query('SELECT COUNT(*) N FROM "FF_FX_API_COST_MONTHLY" WHERE API_ID = ?', ("A01",)).iloc[0]["N"]) == 12
