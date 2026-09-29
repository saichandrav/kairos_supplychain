"""Live external signals for Disruption Sensing: real current marine weather (Open-Meteo, no key
needed) and real current maritime/trade news (NewsAPI.org, needs NEWSAPI_KEY - see docs/deploy.md).

Network calls only; every function returns None/empty on failure or a missing key rather than
inventing a reading, same as every other data source in this app.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
NEWS_URL = "https://newsapi.org/v2/everything"


async def _get_with_retry(client: httpx.AsyncClient, url: str, params: dict[str, Any],
                          headers: dict[str, str] | None = None, attempts: int = 3) -> httpx.Response:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout) as exc:
            last = exc  # transient - Open-Meteo/NewsAPI occasionally drop a fresh connection attempt
    raise last  # type: ignore[misc]


async def marine_conditions(lat: float, lon: float, client: httpx.AsyncClient) -> dict[str, Any] | None:
    """Real current wave height (m) and wind speed (km/h) at a point, via Open-Meteo. No API key required."""
    try:
        wave_resp = await _get_with_retry(client, MARINE_URL, {"latitude": lat, "longitude": lon, "current": "wave_height"})
        wind_resp = await _get_with_retry(client, WEATHER_URL, {"latitude": lat, "longitude": lon, "current": "wind_speed_10m"})
        wave = (wave_resp.json().get("current") or {}).get("wave_height")
        wind = (wind_resp.json().get("current") or {}).get("wind_speed_10m")
        return {"wave_height_m": wave, "wind_speed_kmh": wind}
    except Exception as exc:  # network/parse errors: never crash the caller, just report no data
        log.warning("marine_conditions failed for (%s, %s): %s: %s", lat, lon, type(exc).__name__, exc)
        return None


async def fetch_news(api_key: str, query: str, client: httpx.AsyncClient, page_size: int = 20) -> list[dict[str, Any]]:
    """Real current articles matching `query`, via NewsAPI.org. Empty list with no key or on failure."""
    if not api_key:
        return []
    try:
        resp = await _get_with_retry(
            client, NEWS_URL, {"q": query, "sortBy": "publishedAt", "pageSize": page_size, "language": "en"},
            headers={"X-Api-Key": api_key},
        )
        data = resp.json()
        return [
            {
                "title": a.get("title"), "description": a.get("description"), "url": a.get("url"),
                "source": (a.get("source") or {}).get("name"), "published_at": a.get("publishedAt"),
            }
            for a in data.get("articles", [])
        ]
    except Exception as exc:
        log.warning("fetch_news failed for query %r: %s", query, exc)
        return []
