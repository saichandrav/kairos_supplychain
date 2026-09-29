"""Tests for the Disruption Sensing Agent, sea route matching, and OpenRouter integration."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agents.contracts import (
    AffectedEntity,
    AffectedSeaRoute,
    DisruptionAssessment,
    DisruptionCategory,
    DisruptionSeverity,
    DisruptionSignalIn,
)
from agents.ctx import Settings
from agents.disruption_sensing import (
    assess,
    find_affected_routes,
    load_sea_routes,
    _fallback_assessment,
    _parse_assessment,
)
from api.main import app, get_current_settings
from services.openrouter import OpenRouterError

client = TestClient(app)

SAMPLE_SIGNAL = DisruptionSignalIn(
    signal_id="test-001",
    raw_text="Houthi militants attacked a container vessel near Bab el-Mandeb strait. "
             "The ship was carrying pharmaceutical raw materials from China to Mumbai port. "
             "Shipping companies are rerouting via Cape of Good Hope, adding 12-15 days transit.",
    source="maritime_intelligence_feed",
    source_url="https://example.com/alert/12345",
)

WEATHER_SIGNAL = DisruptionSignalIn(
    signal_id="test-002",
    raw_text="Cyclone Michaung expected to hit Chennai port in 48 hours. "
             "All vessel movements suspended at Chennai and Vizag ports.",
    source="imd_weather_alert",
)

VALID_LLM_RESPONSE = {
    "choices": [{
        "message": {
            "content": json.dumps({
                "event_type": "MARITIME_SECURITY",
                "summary": "Attack on container vessel near Bab el-Mandeb disrupts pharma API shipments from China to India.",
                "description": "Houthi militant attack forces rerouting of vessels carrying pharmaceutical raw materials.",
                "severity": "HIGH",
                "location": "Bab el-Mandeb strait, Red Sea",
                "event_time": None,
                "affected_entities": [
                    {"entity_type": "chokepoint", "entity_name": "Bab el-Mandeb", "confidence": 0.95},
                    {"entity_type": "route", "entity_name": "China-Mumbai sea route", "confidence": 0.9},
                    {"entity_type": "port", "entity_name": "Mumbai port", "confidence": 0.7},
                ],
                "potential_impact": "12-15 day delay on API imports from China via Red Sea. Multiple formulations exposed.",
                "confidence": 0.85,
                "evidence": [
                    "Container vessel attacked near Bab el-Mandeb",
                    "Ship carrying pharmaceutical raw materials from China to Mumbai",
                    "Rerouting via Cape of Good Hope adds 12-15 days",
                ],
                "uncertainties": [
                    "Specific APIs/formulations on the vessel are unknown",
                    "Duration of rerouting disruption is uncertain",
                ],
                "recommended_actions": [
                    "Identify formulations dependent on Chinese-sourced APIs arriving via Red Sea route",
                    "Check buffer stock levels for affected formulations",
                    "Contact alternate Indian API suppliers",
                ],
                "requires_human_review": True,
                "rationale": "HIGH severity due to direct impact on pharmaceutical API supply route with significant transit delay.",
            })
        }
    }]
}

TEST_SETTINGS = Settings(
    db_backend="sqlite",
    openrouter_api_key="test-key-xxx",
    openrouter_model="openai/gpt-4o-mini",
    app_api_key="",
)


@pytest.fixture(autouse=True)
def override_settings():
    app.dependency_overrides[get_current_settings] = lambda: TEST_SETTINGS
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------- Sea route tests


def test_load_sea_routes():
    cfg = load_sea_routes()
    assert "routes" in cfg
    assert "chokepoints" in cfg
    assert "indian_ports" in cfg
    assert len(cfg["routes"]) > 0
    assert "Suez Canal" in cfg["chokepoints"]
    assert "Bab el-Mandeb" in cfg["chokepoints"]


def test_find_routes_bab_el_mandeb():
    entities = [AffectedEntity(entity_type="chokepoint", entity_name="Bab el-Mandeb", confidence=0.9)]
    routes = find_affected_routes("Bab el-Mandeb strait, Red Sea", entities)
    assert len(routes) > 0
    origins = {r["origin"] for r in routes}
    assert "DE" in origins or "IT" in origins or "CH" in origins
    for r in routes:
        if r.get("disrupted_chokepoint") == "Bab el-Mandeb":
            assert r.get("transit_days_rerouted") is not None


def test_find_routes_suez_canal():
    entities = [AffectedEntity(entity_type="chokepoint", entity_name="Suez Canal", confidence=0.9)]
    routes = find_affected_routes("Suez Canal blocked", entities)
    assert len(routes) > 0
    has_reroute = any(r.get("reroute_via") == "Cape of Good Hope" for r in routes)
    assert has_reroute


def test_find_routes_malacca():
    entities = [AffectedEntity(entity_type="chokepoint", entity_name="Strait of Malacca", confidence=0.9)]
    routes = find_affected_routes("Strait of Malacca piracy alert", entities)
    assert len(routes) > 0
    origins = {r["origin"] for r in routes}
    assert "CN" in origins


def test_find_routes_chennai_port():
    entities = [AffectedEntity(entity_type="port", entity_name="Chennai port", confidence=0.9)]
    routes = find_affected_routes("Chennai port closed due to cyclone", entities)
    assert len(routes) > 0
    has_chennai = any("Chennai" in str(r.get("destination_ports", [])) for r in routes)
    assert has_chennai


def test_find_routes_no_match():
    entities = [AffectedEntity(entity_type="location", entity_name="Antarctica", confidence=0.5)]
    routes = find_affected_routes("Antarctic research station", entities)
    assert len(routes) == 0


def test_find_routes_mumbai():
    entities = [AffectedEntity(entity_type="port", entity_name="Mumbai JNPT", confidence=0.9)]
    routes = find_affected_routes("Mumbai port congestion", entities)
    assert len(routes) > 0


# ---------------------------------------------------------------- Agent unit tests


@pytest.mark.asyncio
async def test_successful_classification_with_routes():
    with patch("agents.disruption_sensing.chat_completion", new_callable=AsyncMock) as mock_cc, \
         patch("agents.disruption_sensing.get_db") as mock_db:
        mock_cc.return_value = VALID_LLM_RESPONSE
        mock_db.side_effect = Exception("no db")
        result = await assess(SAMPLE_SIGNAL, settings=TEST_SETTINGS)

    assert isinstance(result, DisruptionAssessment)
    assert result.signal_id == "test-001"
    assert result.event_type == DisruptionCategory.MARITIME_SECURITY
    assert result.severity == DisruptionSeverity.HIGH
    assert result.requires_human_review is True
    assert result.confidence == 0.85
    assert len(result.affected_sea_routes) > 0
    assert result.model_id == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_missing_api_key_still_returns_routes():
    no_key_settings = Settings(db_backend="sqlite", openrouter_api_key="", app_api_key="")
    with patch("agents.disruption_sensing.get_db") as mock_db:
        mock_db.side_effect = Exception("no db")
        result = await assess(SAMPLE_SIGNAL, settings=no_key_settings)

    assert isinstance(result, DisruptionAssessment)
    assert result.confidence == 0.0
    assert result.requires_human_review is True
    assert len(result.affected_sea_routes) > 0


@pytest.mark.asyncio
async def test_malformed_json_returns_fallback():
    bad_response = {"choices": [{"message": {"content": "This is not JSON at all!"}}]}
    with patch("agents.disruption_sensing.chat_completion", new_callable=AsyncMock) as mock_cc, \
         patch("agents.disruption_sensing.get_db") as mock_db:
        mock_cc.return_value = bad_response
        mock_db.side_effect = Exception("no db")
        result = await assess(SAMPLE_SIGNAL, settings=TEST_SETTINGS)

    assert result.confidence == 0.0
    assert result.requires_human_review is True
    assert result.event_type == DisruptionCategory.OTHER


@pytest.mark.asyncio
async def test_provider_timeout_returns_fallback():
    with patch("agents.disruption_sensing.chat_completion", new_callable=AsyncMock) as mock_cc, \
         patch("agents.disruption_sensing.get_db") as mock_db:
        mock_cc.side_effect = OpenRouterError("Network error after 3 attempts: ReadTimeout")
        mock_db.side_effect = Exception("no db")
        result = await assess(SAMPLE_SIGNAL, settings=TEST_SETTINGS)

    assert result.confidence == 0.0
    assert result.requires_human_review is True


@pytest.mark.asyncio
async def test_rate_limit_returns_fallback():
    with patch("agents.disruption_sensing.chat_completion", new_callable=AsyncMock) as mock_cc, \
         patch("agents.disruption_sensing.get_db") as mock_db:
        mock_cc.side_effect = OpenRouterError("Rate limit exceeded after retries")
        mock_db.side_effect = Exception("no db")
        result = await assess(SAMPLE_SIGNAL, settings=TEST_SETTINGS)

    assert result.confidence == 0.0
    assert result.requires_human_review is True


@pytest.mark.asyncio
async def test_invalid_enum_values_handled():
    bad_enums = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "event_type": "NONEXISTENT_CATEGORY",
                    "summary": "Test signal",
                    "severity": "ULTRA_MEGA_BAD",
                    "confidence": 0.5,
                    "requires_human_review": True,
                })
            }
        }]
    }
    with patch("agents.disruption_sensing.chat_completion", new_callable=AsyncMock) as mock_cc, \
         patch("agents.disruption_sensing.get_db") as mock_db:
        mock_cc.return_value = bad_enums
        mock_db.side_effect = Exception("no db")
        result = await assess(SAMPLE_SIGNAL, settings=TEST_SETTINGS)

    assert result.event_type == DisruptionCategory.OTHER
    assert result.severity == DisruptionSeverity.MEDIUM


@pytest.mark.asyncio
async def test_high_severity_forces_human_review():
    high_sev = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "event_type": "GEOPOLITICAL",
                    "summary": "Critical geopolitical event",
                    "severity": "CRITICAL",
                    "confidence": 0.9,
                    "requires_human_review": False,
                })
            }
        }]
    }
    with patch("agents.disruption_sensing.chat_completion", new_callable=AsyncMock) as mock_cc, \
         patch("agents.disruption_sensing.get_db") as mock_db:
        mock_cc.return_value = high_sev
        mock_db.side_effect = Exception("no db")
        result = await assess(SAMPLE_SIGNAL, settings=TEST_SETTINGS)

    assert result.severity == DisruptionSeverity.CRITICAL
    assert result.requires_human_review is True


@pytest.mark.asyncio
async def test_invalid_confidence_clamped():
    bad_conf = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "event_type": "WEATHER",
                    "summary": "Cyclone warning",
                    "severity": "MEDIUM",
                    "confidence": 5.0,
                })
            }
        }]
    }
    with patch("agents.disruption_sensing.chat_completion", new_callable=AsyncMock) as mock_cc, \
         patch("agents.disruption_sensing.get_db") as mock_db:
        mock_cc.return_value = bad_conf
        mock_db.side_effect = Exception("no db")
        result = await assess(SAMPLE_SIGNAL, settings=TEST_SETTINGS)

    assert result.confidence == 0.5


# ---------------------------------------------------------------- API endpoint tests


def test_api_assess_endpoint():
    with patch("agents.disruption_sensing.chat_completion", new_callable=AsyncMock) as mock_cc:
        mock_cc.return_value = VALID_LLM_RESPONSE
        resp = client.post("/disruption/assess", json={
            "signal_id": "api-test-001",
            "raw_text": "Major port congestion at Nhava Sheva due to monsoon flooding.",
            "source": "port_authority",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["signal_id"] == "api-test-001"
    assert data["event_type"] in [c.value for c in DisruptionCategory]
    assert data["requires_human_review"] is True
    assert "affected_sea_routes" in data
    assert "total_affected_formulations" in data


def test_api_assess_requires_fields():
    resp = client.post("/disruption/assess", json={})
    assert resp.status_code == 422


def test_api_assess_rejects_oversized_text():
    resp = client.post("/disruption/assess", json={
        "signal_id": "big-001",
        "raw_text": "x" * 10001,
        "source": "test",
    })
    assert resp.status_code == 422


def test_api_assess_auth_enforced():
    auth_settings = Settings(
        db_backend="sqlite",
        openrouter_api_key="test-key",
        app_api_key="secret-123",
    )
    app.dependency_overrides[get_current_settings] = lambda: auth_settings
    try:
        resp = client.post("/disruption/assess", json={
            "signal_id": "auth-test",
            "raw_text": "Test signal",
            "source": "test",
        })
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_sea_routes_endpoint():
    resp = client.get("/disruption/sea-routes")
    assert resp.status_code == 200
    data = resp.json()
    assert "routes" in data
    assert "chokepoints" in data
    assert "indian_ports" in data
    assert len(data["routes"]) > 0


def test_sea_route_check_endpoint():
    resp = client.get("/disruption/sea-routes/check", params={"location": "Suez Canal"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["location_queried"] == "Suez Canal"
    assert data["affected_routes_count"] > 0
    assert "routes" in data


def test_sea_route_check_no_match():
    resp = client.get("/disruption/sea-routes/check", params={"location": "North Pole"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["affected_routes_count"] == 0


def test_fallback_preserves_routes():
    routes = [AffectedSeaRoute(
        origin="DE", origin_name="Germany",
        origin_ports=["Hamburg"], destination_ports=["Mumbai"],
        chokepoints=["Suez Canal", "Bab el-Mandeb"],
        disrupted_chokepoint="Bab el-Mandeb",
        transit_days_normal=22, transit_days_rerouted=35,
        reroute_via="Cape of Good Hope", delay_days=13,
        affected_formulations=["FORM-001", "FORM-002"],
    )]
    fb = _fallback_assessment(SAMPLE_SIGNAL, "test reason", routes)
    assert fb.confidence == 0.0
    assert fb.requires_human_review is True
    assert len(fb.affected_sea_routes) == 1
    assert fb.total_affected_formulations == 2
    assert fb.affected_sea_routes[0].delay_days == 13
