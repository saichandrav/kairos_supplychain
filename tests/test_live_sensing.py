"""Live Disruption Sensing (agents/live_sensing.py, services/live_signals.py): classification logic
over mocked weather/news responses. No real network calls - offline and deterministic like every
other test in this suite."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from agents import live_sensing, sea_routes
from agents.ctx import Settings


def run(coro):
    return asyncio.run(coro)


def _weather(wave: float | None, wind: float | None) -> dict:
    return {"wave_height_m": wave, "wind_speed_kmh": wind}


def test_high_waves_mark_a_chokepoint_blocked(rules):
    with patch("agents.live_sensing.live_signals.marine_conditions", new=AsyncMock(return_value=_weather(6.0, 20.0))):
        weather = run(live_sensing._weather_scan(rules))
    assert all(w["blocked"] and w["risky"] for w in weather.values())


def test_moderate_waves_mark_risky_not_blocked(rules):
    with patch("agents.live_sensing.live_signals.marine_conditions", new=AsyncMock(return_value=_weather(3.5, 20.0))):
        weather = run(live_sensing._weather_scan(rules))
    assert all(w["risky"] and not w["blocked"] for w in weather.values())


def test_calm_conditions_are_clear(rules):
    with patch("agents.live_sensing.live_signals.marine_conditions", new=AsyncMock(return_value=_weather(0.3, 10.0))):
        weather = run(live_sensing._weather_scan(rules))
    assert all(not w["risky"] and not w["blocked"] for w in weather.values())


def test_failed_reading_is_omitted_not_invented(rules):
    with patch("agents.live_sensing.live_signals.marine_conditions", new=AsyncMock(return_value=None)):
        weather = run(live_sensing._weather_scan(rules))
    assert weather == {}


def test_security_news_flags_the_mentioned_chokepoint_as_risky():
    assert live_sensing._classify("Houthi attack on tanker near Bab el-Mandeb") == "security"


def test_closure_news_flags_blocked():
    assert live_sensing._classify("Suez Canal traffic suspended after grounding") == "closure"


def test_congestion_news_flags_congested():
    assert live_sensing._classify("Severe congestion and delays reported at JNPT") == "congestion"


def test_unrelated_news_is_not_classified():
    assert live_sensing._classify("Local cricket match rained out") is None


def test_scan_combines_weather_and_news_into_a_lane_state(rules):
    settings = Settings(newsapi_key="fake-key")
    news_items = [{"title": "Attack reported near Bab el-Mandeb", "description": "Security incident",
                  "url": "https://example.com/1", "source": "Reuters", "published_at": "2026-09-28T00:00:00Z"}]
    with patch("agents.live_sensing.live_signals.marine_conditions", new=AsyncMock(return_value=_weather(0.2, 10.0))), \
         patch("agents.live_sensing.live_signals.fetch_news", new=AsyncMock(return_value=news_items)):
        result = run(live_sensing.scan(rules, settings))
    assert "Bab el-Mandeb" in result["risky_chokepoints"]
    assert result["news_enabled"] is True
    assert result["news"][0]["classified_as"] == "security"
    state = live_sensing.to_lane_state(result)
    assert isinstance(state, sea_routes.LaneState)
    assert "Bab el-Mandeb" in state.risky_chokepoints


def test_no_newsapi_key_skips_news_but_keeps_weather(rules):
    settings = Settings(newsapi_key="")
    with patch("agents.live_sensing.live_signals.marine_conditions", new=AsyncMock(return_value=_weather(0.2, 10.0))):
        result = run(live_sensing.scan(rules, settings))
    assert result["news_enabled"] is False
    assert result["news"] == []
    assert len(result["weather"]) == 9
