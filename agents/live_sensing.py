"""Live Disruption Sensing: turns real current marine weather (Open-Meteo) and maritime/trade news
(NewsAPI.org, needs NEWSAPI_KEY) into a sea_routes.LaneState the Route Optimizer and Order-a-Medicine
can react to, instead of only the manual what-if chips.

Weather is classified against a fixed threshold (rules/disruption.yaml live_sensing.*); news is
matched to chokepoints/ports/countries with the same whole-phrase matching Disruption Sensing already
uses (sea_routes.match_places) and classified by keyword, not a model - deterministic and auditable.
Every finding carries its source (a weather reading or a headline+url) so nothing here is asserted
without evidence. This never decides anything on its own: it only builds a LaneState for the caller
to price/route against, same as any manually-toggled what-if.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from agents import sea_routes
from agents.ctx import Settings
from agents.rules import Rules
from services import live_signals

SECURITY_KEYWORDS = ("attack", "strike", "houthi", "missile", "seize", "seized", "hijack", "sunk", "sank",
                     "blockade", "war", "conflict", "sanction", "drone", "explosion")
CLOSURE_KEYWORDS = ("closed", "closure", "blocked", "suspend", "halt", "grounded", "collision", "sink")
CONGESTION_KEYWORDS = ("congestion", "congested", "backlog", "delay", "delays", "queue", "queuing", "strike",
                       "shortage", "bottleneck")

NEWS_QUERY = ('("Suez Canal" OR "Bab el-Mandeb" OR "Red Sea" OR "Strait of Malacca" OR "Strait of Hormuz" OR '
             '"Panama Canal" OR "Strait of Gibraltar" OR "Nhava Sheva" OR JNPT OR Chennai OR "port of India") '
             'AND (ship OR shipping OR vessel OR port OR tanker OR container OR tariff)')


def _r(rules: Rules, path: str) -> Any:
    return rules.value("disruption", path)


async def _weather_scan(rules: Rules) -> dict[str, dict[str, Any]]:
    cfg = sea_routes.load()
    coords = {name: info["coords"] for name, info in cfg["chokepoints"].items() if info.get("coords")}
    wave_risky = float(_r(rules, "live_sensing.wave_height_risky_m"))
    wave_blocked = float(_r(rules, "live_sensing.wave_height_blocked_m"))
    wind_risky = float(_r(rules, "live_sensing.wind_speed_risky_kmh"))

    names = list(coords)
    async with httpx.AsyncClient(timeout=10.0) as client:
        results = await asyncio.gather(
            *(live_signals.marine_conditions(coords[name][1], coords[name][0], client) for name in names))
    out: dict[str, dict[str, Any]] = {}
    for name, cond in zip(names, results):
        if not cond:
            continue
        wave, wind = cond.get("wave_height_m"), cond.get("wind_speed_kmh")
        blocked = wave is not None and wave >= wave_blocked
        risky = blocked or (wave is not None and wave >= wave_risky) or (wind is not None and wind >= wind_risky)
        out[name] = {**cond, "blocked": blocked, "risky": risky}
    return out


async def _news_scan(rules: Rules, settings: Settings) -> list[dict[str, Any]]:
    if not settings.newsapi_key:
        return []
    page_size = int(_r(rules, "live_sensing.news_page_size"))
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await live_signals.fetch_news(settings.newsapi_key, NEWS_QUERY, client, page_size=page_size)


def _classify(text: str) -> str | None:
    t = text.lower()
    if any(k in t for k in SECURITY_KEYWORDS):
        return "security"
    if any(k in t for k in CLOSURE_KEYWORDS):
        return "closure"
    if any(k in t for k in CONGESTION_KEYWORDS):
        return "congestion"
    return None


async def scan(rules: Rules, settings: Settings) -> dict[str, Any]:
    """Real current weather + news, turned into blocked/risky chokepoints and blocked/congested ports."""
    weather = await _weather_scan(rules)
    news = await _news_scan(rules, settings)

    blocked_chokepoints: set[str] = {n for n, w in weather.items() if w["blocked"]}
    risky_chokepoints: set[str] = {n for n, w in weather.items() if w["risky"]}
    blocked_ports: set[str] = set()
    congested_ports: set[str] = set()
    matched_news: list[dict[str, Any]] = []

    for item in news:
        text = f"{item.get('title') or ''} {item.get('description') or ''}"
        places = sea_routes.match_places(text)
        kind = _classify(text)
        if not places or kind is None:
            continue
        if kind == "security":
            risky_chokepoints |= places.chokepoints
        elif kind == "closure":
            blocked_chokepoints |= places.chokepoints
            blocked_ports |= places.indian_ports
        elif kind == "congestion":
            congested_ports |= places.indian_ports
            risky_chokepoints |= places.chokepoints
        matched_news.append({
            **item, "matched_chokepoints": sorted(places.chokepoints), "matched_ports": sorted(places.indian_ports),
            "matched_countries": sorted(places.countries), "classified_as": kind,
        })

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "blocked_chokepoints": sorted(blocked_chokepoints),
        "risky_chokepoints": sorted(risky_chokepoints - blocked_chokepoints),
        "blocked_ports": sorted(blocked_ports),
        "congested_ports": sorted(congested_ports - blocked_ports),
        "weather": weather, "news": matched_news, "news_enabled": bool(settings.newsapi_key),
    }


def to_lane_state(live: dict[str, Any], duration_days: int = 14) -> sea_routes.LaneState:
    """A LaneState built from a live scan, ready to pass into shipment.quote()/suppliers.rank_suppliers()."""
    return sea_routes.LaneState(
        blocked_chokepoints=set(live["blocked_chokepoints"]), risky_chokepoints=set(live["risky_chokepoints"]),
        blocked_ports=set(live["blocked_ports"]), congested_ports=set(live["congested_ports"]),
        duration_days=duration_days)
