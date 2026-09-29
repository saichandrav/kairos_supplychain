"""Route Optimizer quoting: price and map every sea/air option for a new inbound API consignment
before it becomes a PO (uses rules/sea_routes.yaml + rules/disruption.yaml logistics.*).

This is a quoting tool, not a procurement action: pure functions over reference data and rules,
no DB writes, no PO, no human gate. It reuses the same lane-option and pricing logic as the
Rerouting & Logistics agent (agents/logistics.py) so a live quote and a disrupted-PO re-plan agree
on cost. The optional `state` lets the caller overlay a hypothetical disruption (blocked/risky
chokepoints or ports) so the UI can show recommendations changing live as a what-if is toggled.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any

from agents import sea_routes
from agents.rules import Rules

AGENT = "SHIPMENT_QUOTE"


def _money(x: float) -> Decimal:
    return Decimal(f"{x:.4f}")


def _r(rules: Rules, path: str) -> Any:
    return rules.value("disruption", path)


@dataclass(frozen=True)
class RouteQuote:
    option_id: str
    label: str
    kind: str  # PRIMARY | ALT_ROUTE | REROUTE | HOLD | AIR
    mode: str  # SEA | AIR
    carrier: str
    via: tuple[str, ...]
    transit_days: int
    extra_days: int
    eta: date
    freight_cost_inr: Decimal
    cost_per_unit_inr: Decimal
    security_risk: bool
    recommended: bool
    origin_port: str | None
    destination_port: str | None
    distance_km: float | None
    geometry: list[list[float]] | None  # [[lon, lat], ...] for Leaflet


@lru_cache(maxsize=1)
def port_coords() -> dict[str, tuple[float, float]]:
    cfg = sea_routes.load()
    return {name: (float(c[0]), float(c[1])) for name, c in (cfg.get("port_coordinates") or {}).items()}


@lru_cache(maxsize=256)
def _searoute_geometry(o_lon: float, o_lat: float, d_lon: float, d_lat: float) -> tuple[list[list[float]] | None, float | None]:
    """Actual sea-lane geometry between two ports (cached; searoute walks a fixed maritime network)."""
    try:
        import searoute as sr
        feat = sr.searoute((o_lon, o_lat), (d_lon, d_lat))
        coords = feat["geometry"]["coordinates"]
        return [[float(x), float(y)] for x, y in coords], float(feat["properties"]["length"])
    except Exception:
        return None, None


def _route_port_lists(origin: str, option_id: str) -> tuple[list[str], list[str]]:
    """(origin ports, destination ports) mapped for the route dict backing a lane_options option_id."""
    routes = sea_routes.routes_for(origin)
    idx_part = option_id.split("-R", 1)[1] if "-R" in option_id else "0"
    idx = int("".join(ch for ch in idx_part if ch.isdigit()) or 0)
    route = routes[idx] if idx < len(routes) else (routes[0] if routes else {})
    return list(route.get("origin_ports") or []), list(route.get("destination_ports") or [])


def _port_distance_km(o_port: str, d_port: str) -> float:
    coords = port_coords()
    if o_port not in coords or d_port not in coords:
        return float("inf")
    _, dist = _searoute_geometry(*coords[o_port], *coords[d_port])
    return dist if dist is not None else float("inf")


def _choose_ports(o_ports: list[str], d_ports: list[str], preferred_destination: str | None,
                   with_geometry: bool) -> tuple[str | None, str | None]:
    """The origin/destination port pair with the shortest real sea distance for this route.

    Multiple mapped ports per origin/destination are common (e.g. China: Shanghai, Ningbo,
    Tianjin, Qingdao); always defaulting to the first listed would be an arbitrary, static
    choice, so this compares actual searoute distance across every candidate pair instead.
    """
    if not o_ports or not d_ports:
        return (o_ports[0] if o_ports else None), (d_ports[0] if d_ports else None)
    d_candidates = [preferred_destination] if preferred_destination in d_ports else d_ports
    if not with_geometry:
        return o_ports[0], d_candidates[0]
    pairs = [(o, d) for o in o_ports for d in d_candidates]
    return min(pairs, key=lambda p: _port_distance_km(*p))


def _geometry_for(origin_port: str | None, destination_port: str | None) -> tuple[list[list[float]] | None, float | None]:
    coords = port_coords()
    if not origin_port or not destination_port or origin_port not in coords or destination_port not in coords:
        return None, None
    (o_lon, o_lat), (d_lon, d_lat) = coords[origin_port], coords[destination_port]
    return _searoute_geometry(o_lon, o_lat, d_lon, d_lat)


def quote(origin: str, qty: float, unit_value_inr: float, as_of: date, rules: Rules,
          state: sea_routes.LaneState | None = None, destination_port: str | None = None,
          with_geometry: bool = True) -> list[RouteQuote]:
    """Every priced, ETA'd option for shipping `qty` units at `unit_value_inr` each from `origin`.

    Cheapest open, non-security-risk option is marked recommended. Blocked options are dropped
    (nothing to book); a HOLD option (wait for the lane to reopen) is kept as a visible fallback.
    """
    if not sea_routes.routes_for(origin):
        return []
    state = state or sea_routes.LaneState()
    k = sea_routes.consts_from_rules(rules)
    value = float(qty) * float(unit_value_inr)
    base_pct = float(_r(rules, "logistics.sea_freight_pct_of_value"))
    per_day = float(_r(rules, "logistics.reroute_surcharge_pct_per_day"))
    war = float(_r(rules, "logistics.war_risk_surcharge_pct"))
    air_pct = float(_r(rules, "logistics.air_freight_pct_of_value"))
    air_max = float(_r(rules, "logistics.air_max_consignment_value_inr"))
    air_days = int(_r(rules, "scenario.air_transit_days"))
    carriers = _r(rules, "logistics.carriers")

    opts = [o for o in sea_routes.lane_options(origin, state, k) if not o.blocked]
    quotes: list[RouteQuote] = []
    for o in opts:
        o_ports, d_ports = _route_port_lists(origin, o.option_id)
        o_port, d_port = _choose_ports(o_ports, d_ports, destination_port, with_geometry)
        surcharge_pct = per_day * max(0, o.extra_days) + (war if o.security_risk else 0.0)
        freight = value * (base_pct + surcharge_pct)
        eta = as_of + timedelta(days=o.transit_days)
        geometry, distance_km = _geometry_for(o_port, d_port) if with_geometry else (None, None)
        best_car = min((c for c in carriers if c["mode"] == "SEA"
                        and not (o.security_risk and c["avoids_security_risk"])),
                       key=lambda c: float(c["cost_factor"]), default=None)
        if best_car is None:
            continue
        cost = freight * float(best_car["cost_factor"])
        quotes.append(RouteQuote(
            option_id=o.option_id, label=o.label, kind=o.kind, mode="SEA", carrier=best_car["id"],
            via=o.via, transit_days=o.transit_days, extra_days=o.extra_days, eta=eta,
            freight_cost_inr=_money(cost), cost_per_unit_inr=_money(cost / qty if qty else 0),
            security_risk=o.security_risk, recommended=False,
            origin_port=o_port, destination_port=d_port, distance_km=distance_km, geometry=geometry))

    if value <= air_max:
        air_car = min((c for c in carriers if c["mode"] == "AIR"), key=lambda c: float(c["cost_factor"]), default=None)
        if air_car is not None:
            o_ports, d_ports = _route_port_lists(origin, f"{origin}-R0")
            o_port, d_port = _choose_ports(o_ports, d_ports, destination_port, with_geometry)
            eta = max(as_of + timedelta(days=air_days), as_of)
            cost = value * air_pct * float(air_car["cost_factor"])
            geometry, distance_km = _geometry_for(o_port, d_port) if with_geometry else (None, None)
            quotes.append(RouteQuote(
                option_id=f"{origin}-AIR", label="Air freight (expedite)", kind="AIR", mode="AIR",
                carrier=air_car["id"], via=(), transit_days=air_days, extra_days=0, eta=eta,
                freight_cost_inr=_money(cost), cost_per_unit_inr=_money(cost / qty if qty else 0),
                security_risk=False, recommended=False,
                origin_port=o_port, destination_port=d_port, distance_km=distance_km, geometry=geometry))

    quotes.sort(key=lambda q: (q.kind == "HOLD", float(q.freight_cost_inr), q.transit_days))
    safe = [q for q in quotes if not q.security_risk and q.kind != "HOLD"]
    best = (safe or quotes)[0] if quotes else None
    return [replace(q, recommended=True) if best and q.option_id == best.option_id else q for q in quotes]


def destination_ports_for(origin: str) -> list[str]:
    """Every Indian port some mapped route for this origin can actually discharge at.

    Not every origin reaches every Indian port (e.g. only China/Japan lanes are mapped to
    Vizag) - the order form uses this to only offer a destination the origin can actually
    reach, instead of silently substituting a different port when the pick isn't served.
    """
    return sorted({p for r in sea_routes.routes_for(origin) for p in r.get("destination_ports", [])})


def origin_summary() -> list[dict[str, Any]]:
    """Origins with their ports, coordinates and reachable Indian ports, for the order form and map."""
    coords = port_coords()
    out = []
    for code in sea_routes.origins():
        routes = sea_routes.routes_for(code)
        ports = sorted({p for r in routes for p in r.get("origin_ports", [])})
        out.append({
            "code": code, "name": sea_routes.origin_name(code),
            "ports": [{"name": p, "coords": coords.get(p)} for p in ports],
            "destination_ports": destination_ports_for(code),
        })
    return out


def destination_summary() -> list[dict[str, Any]]:
    coords = port_coords()
    cfg = sea_routes.load()
    return [{"name": name, "city": info.get("city"), "state": info.get("state"), "coords": coords.get(name)}
            for name, info in cfg["indian_ports"].items()]
