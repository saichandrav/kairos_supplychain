"""Order-a-Medicine supplier ranking: combines a seeded demo supplier's price and reliability with a
live Route Optimizer freight quote (agents/shipment.py) for their country, to recommend the best
overall source for a medicine and show every candidate's route on the map.

Suppliers here are a SYNTH demo panel (data/demo/suppliers.json), not a live vendor feed - each
medicine's `source`/`is_proxy` fields say so, same as any other proxy data row in this app. Like
agents/shipment.py, this is a comparison tool: no DB writes, no PO, no human gate.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from agents import sea_routes, shipment
from agents.rules import Rules

PATH = Path(__file__).resolve().parent.parent / "data" / "demo" / "suppliers.json"


def _money(x: float) -> Decimal:
    return Decimal(f"{x:.4f}")


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    with PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _medicine(medicine_id: str) -> dict[str, Any] | None:
    return next((m for m in _load()["medicines"] if m["medicine_id"] == medicine_id), None)


def medicines() -> list[dict[str, Any]]:
    """Seeded demo medicines with a supplier count, for the picker."""
    return [{"medicine_id": m["medicine_id"], "name": m["name"], "generic": m.get("generic"),
             "supplier_count": len(m["suppliers"])} for m in _load()["medicines"]]


@dataclass(frozen=True)
class SupplierOffer:
    supplier_id: str
    name: str
    country: str
    country_name: str
    city: str
    unit_price_inr: Decimal
    reliability: float
    lead_time_days: int
    certification: str
    route: shipment.RouteQuote | None
    landed_cost_inr: Decimal
    total_days: int
    recommended: bool


def _score(o: SupplierOffer) -> float:
    """Risk-adjusted landed cost: cheaper AND more reliable ranks first (never a black-box score -
    it's exactly landed cost / reliability, shown in full in the UI)."""
    return float(o.landed_cost_inr) / max(o.reliability, 0.01)


def rank_suppliers(medicine_id: str, qty: float, as_of: date, rules: Rules,
                   state: sea_routes.LaneState | None = None, with_geometry: bool = True) -> dict[str, Any] | None:
    """Every seeded supplier for a medicine, priced and routed, ranked by risk-adjusted landed cost."""
    med = _medicine(medicine_id)
    if med is None or not med.get("suppliers"):
        return None
    offers: list[SupplierOffer] = []
    for s in med["suppliers"]:
        unit_price = float(s["unit_price_inr"])
        quotes = shipment.quote(s["country"], qty, unit_price, as_of, rules, state,
                                destination_port=s.get("destination_port"), with_geometry=with_geometry)
        route = next((q for q in quotes if q.recommended), quotes[0] if quotes else None)
        freight = float(route.freight_cost_inr) if route else 0.0
        landed = qty * unit_price + freight
        lead = int(s.get("lead_time_days", 0))
        offers.append(SupplierOffer(
            supplier_id=s["supplier_id"], name=s["name"], country=s["country"],
            country_name=sea_routes.origin_name(s["country"]), city=s["city"],
            unit_price_inr=_money(unit_price), reliability=float(s["reliability"]), lead_time_days=lead,
            certification=s.get("certification", ""), route=route, landed_cost_inr=_money(landed),
            total_days=lead + (route.transit_days if route else 0), recommended=False))
    if not offers:
        return None
    best = min(offers, key=_score)
    offers = sorted((replace(o, recommended=o is best) for o in offers), key=_score)
    return {"medicine_id": medicine_id, "name": med["name"], "generic": med.get("generic"), "qty": qty,
            "source": med.get("source") or _load().get("source"), "is_proxy": med.get("is_proxy") or _load().get("is_proxy"),
            "offers": offers}
