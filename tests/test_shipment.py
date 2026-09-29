"""Route Optimizer quoting (agents/shipment.py): pure functions over sea_routes + disruption.yaml logistics.*."""
from __future__ import annotations

from datetime import date

from agents import sea_routes, shipment

AS_OF = date(2026, 9, 28)


def test_quote_recommends_the_cheapest_open_safe_option(rules):
    quotes = shipment.quote("CN", qty=1000, unit_value_inr=250.0, as_of=AS_OF, rules=rules, with_geometry=False)
    assert quotes, "China has mapped lanes"
    recommended = [q for q in quotes if q.recommended]
    assert len(recommended) == 1
    assert recommended[0] is min(quotes, key=lambda q: float(q.freight_cost_inr))
    assert all(float(q.freight_cost_inr) > 0 for q in quotes)
    assert all(q.eta > AS_OF for q in quotes)


def test_risky_chokepoint_moves_the_recommendation_to_the_bypass(rules):
    state = sea_routes.LaneState(risky_chokepoints={"Strait of Malacca"})
    quotes = shipment.quote("CN", qty=1000, unit_value_inr=250.0, as_of=AS_OF, rules=rules, state=state,
                            with_geometry=False)
    recommended = next(q for q in quotes if q.recommended)
    assert not recommended.security_risk
    primary = next(q for q in quotes if q.option_id == "CN-R0")
    assert primary.security_risk
    assert not primary.recommended


def test_blocked_chokepoint_drops_the_primary_lane_entirely(rules):
    state = sea_routes.LaneState(blocked_chokepoints={"Strait of Malacca"})
    quotes = shipment.quote("CN", qty=1000, unit_value_inr=250.0, as_of=AS_OF, rules=rules, state=state,
                            with_geometry=False)
    assert all(q.option_id != "CN-R0" for q in quotes)


def test_high_value_consignment_has_no_air_option(rules):
    quotes = shipment.quote("CN", qty=100, unit_value_inr=10_000.0, as_of=AS_OF, rules=rules, with_geometry=False)
    assert all(q.mode != "AIR" for q in quotes)


def test_unmapped_origin_returns_no_quotes(rules):
    assert shipment.quote("ZZ", qty=100, unit_value_inr=10.0, as_of=AS_OF, rules=rules, with_geometry=False) == []


def test_geometry_and_distance_are_populated_for_known_ports(rules):
    quotes = shipment.quote("CN", qty=1000, unit_value_inr=250.0, as_of=AS_OF, rules=rules)
    primary = next(q for q in quotes if q.option_id == "CN-R0")
    assert primary.geometry and len(primary.geometry) > 1
    assert primary.distance_km and primary.distance_km > 0


def test_origin_and_destination_summary_expose_coordinates():
    origins = shipment.origin_summary()
    assert any(o["code"] == "CN" for o in origins)
    cn = next(o for o in origins if o["code"] == "CN")
    assert all(p["coords"] is not None for p in cn["ports"])

    destinations = shipment.destination_summary()
    names = {d["name"] for d in destinations}
    assert "JNPT/Nhava Sheva" in names
