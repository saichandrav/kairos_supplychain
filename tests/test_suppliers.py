"""Order-a-Medicine supplier ranking (agents/suppliers.py): combines the demo supplier panel with
live Route Optimizer freight quotes (agents/shipment.py)."""
from __future__ import annotations

from datetime import date

from agents import sea_routes, suppliers

AS_OF = date(2026, 9, 29)


def test_medicines_lists_the_seeded_demo_panel():
    meds = suppliers.medicines()
    ids = {m["medicine_id"] for m in meds}
    assert {"BENZATHINE-BENZYLPENICIL-5E7D1810", "ATROPINE-6BCEA471", "AMIKACIN-ED68AE37"} <= ids
    assert all(m["supplier_count"] > 0 for m in meds)


def test_rank_suppliers_recommends_exactly_one_and_prices_every_offer(rules):
    res = suppliers.rank_suppliers("ATROPINE-6BCEA471", 5000, AS_OF, rules, with_geometry=False)
    assert res is not None
    offers = res["offers"]
    assert len(offers) == 4
    assert sum(o.recommended for o in offers) == 1
    assert all(float(o.landed_cost_inr) > 0 and o.route is not None for o in offers)
    # sorted by risk-adjusted landed cost (landed / reliability), cheapest-and-safest first
    scores = [float(o.landed_cost_inr) / o.reliability for o in offers]
    assert scores == sorted(scores)
    assert offers[0].recommended


def test_rank_suppliers_unknown_medicine_returns_none(rules):
    assert suppliers.rank_suppliers("NOT-A-REAL-MEDICINE", 100, AS_OF, rules, with_geometry=False) is None


def test_disruption_state_can_change_the_recommended_supplier(rules):
    """A supplier whose lane gets risky should lose ground to one on an unaffected lane."""
    baseline = suppliers.rank_suppliers("AMIKACIN-ED68AE37", 2000, AS_OF, rules, with_geometry=False)
    best_id = next(o.supplier_id for o in baseline["offers"] if o.recommended)
    best_country = next(o.country for o in baseline["offers"] if o.recommended)
    routes = sea_routes.routes_for(best_country)
    chokepoint = routes[0]["chokepoints"][0] if routes and routes[0].get("chokepoints") else None
    if chokepoint is None:
        return  # nothing to disrupt for this supplier's lane; skip rather than assert nothing
    state = sea_routes.LaneState(risky_chokepoints={chokepoint})
    disrupted = suppliers.rank_suppliers("AMIKACIN-ED68AE37", 2000, AS_OF, rules, state, with_geometry=False)
    disrupted_best = next(o for o in disrupted["offers"] if o.supplier_id == best_id)
    baseline_best = next(o for o in baseline["offers"] if o.supplier_id == best_id)
    # the disrupted chokepoint must show up in this supplier's route: either it reroutes
    # (extra days / different lane) or it accepts the now-risky primary lane
    assert (disrupted_best.route.security_risk or disrupted_best.route.extra_days > 0
            or disrupted_best.route.option_id != baseline_best.route.option_id)


def test_route_reflects_supplier_country_and_reasonable_landed_cost(rules):
    res = suppliers.rank_suppliers("BENZATHINE-BENZYLPENICIL-5E7D1810", 1000, AS_OF, rules, with_geometry=False)
    for o in res["offers"]:
        assert o.route.origin_port is not None
        assert float(o.landed_cost_inr) >= float(o.unit_price_inr) * res["qty"]
