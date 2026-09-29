"""Sea-lane reference data (rules/sea_routes.yaml): place matching and lane options under a disruption.

Shared by Disruption Sensing (which places a signal names), Scenario Planning (lane delays)
and Rerouting & Logistics (route choices). Pure functions over the YAML; no DB access.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PATH = Path(__file__).resolve().parent.parent / "rules" / "sea_routes.yaml"


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    with PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _found(text_lc: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])", text_lc) is not None


def _names(name: str, info: dict[str, Any]) -> list[str]:
    return [name, *(info.get("aliases") or [])]


@dataclass(frozen=True)
class Places:
    chokepoints: frozenset[str] = frozenset()
    indian_ports: frozenset[str] = frozenset()
    origin_ports: frozenset[str] = frozenset()
    countries: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.chokepoints or self.indian_ports or self.origin_ports or self.countries)


def match_places(text: str) -> Places:
    """Whole-phrase match of chokepoints, ports and origin countries (with aliases) in free text."""
    cfg, t = load(), (text or "").lower()
    cps = {n for n, i in cfg["chokepoints"].items() if any(_found(t, a) for a in _names(n, i))}
    ports = {n for n, i in cfg["indian_ports"].items() if any(_found(t, a) for a in _names(n, i))}
    origin_ports = {p for r in cfg["routes"] for p in r.get("origin_ports", []) if _found(t, p)}
    countries = {c for c, names in cfg["countries"].items() if any(_found(t, n) for n in names)}
    return Places(frozenset(cps), frozenset(ports), frozenset(origin_ports), frozenset(countries))


def routes_for(origin: str) -> list[dict[str, Any]]:
    """Routes for an origin country; the first is the primary lane."""
    return [r for r in load()["routes"] if r["origin"] == origin]


def origins() -> list[str]:
    return sorted({r["origin"] for r in load()["routes"]})


def origin_name(origin: str) -> str:
    rs = routes_for(origin)
    return rs[0]["origin_name"] if rs else origin


@dataclass
class LaneState:
    """What a disruption does to the network."""

    blocked_chokepoints: set[str] = field(default_factory=set)
    risky_chokepoints: set[str] = field(default_factory=set)
    blocked_ports: set[str] = field(default_factory=set)
    congested_ports: set[str] = field(default_factory=set)
    duration_days: int = 30

    def touches(self, route: dict[str, Any]) -> bool:
        cps = set(route.get("chokepoints", []))
        ports = set(route.get("destination_ports", []))
        return bool(cps & (self.blocked_chokepoints | self.risky_chokepoints)
                    or ports & (self.blocked_ports | self.congested_ports))


@dataclass(frozen=True)
class LaneOption:
    option_id: str
    origin: str
    kind: str  # PRIMARY | ALT_ROUTE | REROUTE | HOLD
    label: str
    transit_days: int
    extra_days: int
    via: tuple[str, ...]
    security_risk: bool
    blocked: bool


@dataclass(frozen=True)
class LaneConsts:
    risky_hold_days: int
    port_divert_days: int
    congestion_days: int
    unrouted_reroute_days: int


def consts_from_rules(rules: Any) -> LaneConsts:
    v = lambda p: int(rules.value("disruption", f"scenario.{p}"))  # noqa: E731
    return LaneConsts(v("risky_route_hold_days"), v("port_divert_days"), v("congestion_delay_days"),
                      v("unrouted_reroute_delay_days"))


def _port_delay(route: dict[str, Any], s: LaneState, k: LaneConsts) -> tuple[int, bool]:
    """(extra days, all destination ports closed) for discharging this route in India."""
    dest = route.get("destination_ports", [])
    open_ports = [p for p in dest if p not in s.blocked_ports]
    if not open_ports:
        return 0, True
    extra = k.port_divert_days if len(open_ports) < len(dest) else 0
    if all(p in s.congested_ports for p in open_ports):
        extra = max(extra, k.congestion_days)
    return extra, False


def _bypass(route: dict[str, Any], bad: list[str], k: LaneConsts) -> tuple[int, list[str], str]:
    """Extra days, new chokepoint list and label for sailing around the `bad` chokepoints of a route."""
    cps_cfg = load()["chokepoints"]
    via = route.get("reroute_via")
    if via and all(cps_cfg.get(cp, {}).get("bypass_via") == via for cp in bad) and route.get("transit_days_rerouted"):
        kept = [cp for cp in route["chokepoints"] if cps_cfg.get(cp, {}).get("bypass_via") != via]
        return int(route["transit_days_rerouted"]) - int(route["transit_days_normal"]), kept + [via], f"Reroute via {via}"
    extra, kept, labels = 0, list(route["chokepoints"]), []
    for cp in bad:
        info = cps_cfg.get(cp, {})
        alt = info.get("bypass_via")
        extra += int(info.get("bypass_extra_days") or k.unrouted_reroute_days)
        kept = [alt if c == cp and alt else c for c in kept if c != cp or alt]
        labels.append(alt or f"an alternate to {cp}")
    return extra, kept, "Reroute via " + " and ".join(labels)


def lane_options(origin: str, s: LaneState, k: LaneConsts) -> list[LaneOption]:
    """Every sea option for an origin: each mapped lane as-is, rerouted around bad chokepoints, or held."""
    routes = routes_for(origin)
    if not routes:
        return []
    base = int(routes[0]["transit_days_normal"])
    out: list[LaneOption] = []
    for i, r in enumerate(routes):
        cps = list(r.get("chokepoints", []))
        port_extra, ports_closed = _port_delay(r, s, k)
        risky = any(cp in s.risky_chokepoints for cp in cps)
        blocked = ports_closed or any(cp in s.blocked_chokepoints for cp in cps)
        transit = int(r["transit_days_normal"]) + port_extra + (k.risky_hold_days if risky else 0)
        kind = "PRIMARY" if i == 0 else "ALT_ROUTE"
        label = ("Primary lane" if i == 0 else "Alternate lane") + (f" via {', '.join(cps)}" if cps else "")
        out.append(LaneOption(f"{origin}-R{i}", origin, kind, label, transit, transit - base, tuple(cps), risky, blocked))

        bad = [cp for cp in cps if cp in s.blocked_chokepoints or cp in s.risky_chokepoints]
        if bad and not ports_closed:
            extra, new_cps, rlabel = _bypass(r, bad, k)
            rt = int(r["transit_days_normal"]) + extra + port_extra
            r_blocked = any(cp in s.blocked_chokepoints for cp in new_cps)
            r_risky = any(cp in s.risky_chokepoints for cp in new_cps)
            out.append(LaneOption(f"{origin}-R{i}-BYPASS", origin, "REROUTE", rlabel, rt, rt - base,
                                  tuple(new_cps), r_risky, r_blocked))
        if i == 0 and blocked:
            hold = int(r["transit_days_normal"]) + s.duration_days + port_extra
            out.append(LaneOption(f"{origin}-R0-HOLD", origin, "HOLD", "Hold cargo until the lane reopens",
                                  hold, hold - base, tuple(cps), risky, False))
    return out


def absorb_option(opts: list[LaneOption]) -> LaneOption | None:
    """What happens with no intervention: the primary lane if open, else hold until it reopens."""
    for o in opts:
        if o.kind == "PRIMARY" and not o.blocked:
            return o
    return next((o for o in opts if o.kind == "HOLD"), None)


def best_reroute(opts: list[LaneOption]) -> LaneOption | None:
    """Fastest open option that avoids elevated-security chokepoints (other than doing nothing)."""
    cands = [o for o in opts if o.kind in ("REROUTE", "ALT_ROUTE") and not o.blocked and not o.security_risk]
    return min(cands, key=lambda o: (o.extra_days, o.option_id)) if cands else None
