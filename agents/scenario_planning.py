"""Scenario Planning Agent: "what if" simulation of a disruption against the hospital's stocked formulations.

For each what-if (a chokepoint closes or turns risky, an Indian port closes or congests, a tariff rises,
a supplier goes offline, an origin stops exports) the agent:
  1. maps every stocked formulation to its API origin lanes (FF_REF_FORM_API, FF_REF_API_ORIGIN,
     rules/sea_routes.yaml) and turns the disruption into days of delay, supply loss or tariff uplift;
  2. evaluates ABSORB, REROUTE, AIR_EXPEDITE, BUFFER_STOCK and ALT_SOURCING per formulation on incremental
     cost (INR), time-to-effect (days) and VED-weighted residual stock-out risk (rules/disruption.yaml);
  3. rolls them up into plan-level options and a recommended per-formulation mix.

The LLM (OpenRouter) only parses free-text what-if questions and writes the narrative. Every number is
computed here; a narrative that cites a number not in the fact sheet is discarded for the template.
"""
from __future__ import annotations

import json
import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

import pandas as pd

from agents import sea_routes
from agents.common import as_of_date, db_ts, po_lines, resolve_ctx, try_query, upsert
from agents.contracts import (
    DataSourceRef, DisruptionAssessment, DisruptionCategory, DisruptionSeverity, FormulationImpact,
    LaneExposure, PlanOption, ScenarioPlan, SignalBand, StrategyEval, StrategyType, WhatIfKind, WhatIfSpec,
)
from agents.ctx import Db, Settings
from agents.explain import verify_numbers
from agents.rules import Rules
from services.openrouter import OpenRouterError, chat_completion, extract_content, extract_json

log = logging.getLogger(__name__)

AGENT = "DX2_SCENARIO_PLAN"
CLOSURE_WORDS = ("closed", "closure", "closes", "blocked", "blockade", "shut", "shutdown", "suspended", "suspend",
                 "halted", "halt")
EXPORT_WORDS = ("export ban", "export stop", "export restriction", "export curb", "halt exports", "halts exports",
                "stops exports", "stop exports", "embargo")
TARIFF_WORDS = ("tariff", "import duty", "customs duty", "anti-dumping", "duty hike")
SUPPLIER_WORDS = ("supplier", "vendor", "distributor")
OFFLINE_WORDS = ("offline", "outage", "strike", "fire", "insolvent", "bankrupt", "shutdown", "shut", "down",
                 "suspended", "closed")
STRATEGY_ORDER = list(StrategyType)
SOURCES = [
    DataSourceRef(source="FF_V_DAYS_OF_COVER", is_proxy="SYNTH"),
    DataSourceRef(source="FF_REF_API_ORIGIN", is_proxy="PROXY"),
    DataSourceRef(source="FF_MM_EKPO", is_proxy="SYNTH"),
    DataSourceRef(source="rules/sea_routes.yaml", is_proxy="PROXY"),
    DataSourceRef(source="rules/disruption.yaml", is_proxy="PROXY"),
]


def _r(rules: Rules, path: str) -> Any:
    return rules.value("disruption", path)


def _money(x: float) -> Decimal:
    return Decimal(f"{x:.4f}")


# ---------------------------------------------------------------- specs


def resolve_target(kind: WhatIfKind, target: str, db: Db | None = None) -> str:
    """Canonical target name for a what-if, or ValueError if it names nothing we know."""
    cfg, t = sea_routes.load(), target.strip()
    if kind in (WhatIfKind.CHOKEPOINT_CLOSURE, WhatIfKind.CHOKEPOINT_RISK):
        exact = {n.lower(): n for n in cfg["chokepoints"]}
        found = [exact[t.lower()]] if t.lower() in exact else sorted(sea_routes.match_places(t).chokepoints)
    elif kind in (WhatIfKind.PORT_CLOSURE, WhatIfKind.PORT_CONGESTION):
        exact = {n.lower(): n for n in cfg["indian_ports"]}
        found = [exact[t.lower()]] if t.lower() in exact else sorted(sea_routes.match_places(t).indian_ports)
    elif kind in (WhatIfKind.TARIFF_INCREASE, WhatIfKind.ORIGIN_EXPORT_STOP):
        if kind is WhatIfKind.TARIFF_INCREASE and t.upper() in ("ALL", "ANY", "*"):
            return "ALL"
        found = [t.upper()] if t.upper() in sea_routes.origins() else sorted(sea_routes.match_places(t).countries)
    else:
        found = _match_vendor(t, db) if db is not None else ([t.upper()] if re.fullmatch(r"[\w-]{1,40}", t) else [])
    if not found:
        raise ValueError(f"unknown {kind.value} target {target!r}")
    return found[0]


def _vendors(db: Db) -> pd.DataFrame:
    df = try_query(db, "SELECT LIFNR, NAME1, SPERR FROM FF_MM_LFA1")
    return df if df is not None else pd.DataFrame(columns=["LIFNR", "NAME1", "SPERR"])


GENERIC_VENDOR_WORDS = {"pharma", "pharmaceuticals", "pharmaceutical", "distributors", "distributor", "medical",
                        "agencies", "healthcare", "supplies", "surgicals", "drug", "drugs", "house", "traders", "synth",
                        "limited", "ltd", "private", "pvt", "and", "the"}


def _match_vendor(text: str, db: Db) -> list[str]:
    """Vendor ids named in the text: an exact LIFNR, or a distinctive word of the vendor name (not 'Pharma')."""
    v = _vendors(db)
    t = text.strip().lower()
    ids = [str(x) for x in v["LIFNR"] if re.search(rf"(?<![a-z0-9]){re.escape(str(x).lower())}(?![a-z0-9])", t)]
    if ids:
        return ids
    words = set(re.findall(r"[a-z]+", t))
    return [str(r.LIFNR) for r in v.itertuples()
            if words & (set(re.findall(r"[a-z]+", str(r.NAME1).lower())) - GENERIC_VENDOR_WORDS)]


def key_supplier(db: Db) -> str | None:
    """The unblocked vendor with the highest PO value in the order history."""
    po = po_lines(db)
    if po.empty:
        return None
    blocked = set(_vendors(db).query("SPERR == 'X'")["LIFNR"].astype(str))
    po = po[~po["LIFNR"].astype(str).isin(blocked)]
    val = (po["MENGE"] * po["UNIT_NET"].fillna(0)).groupby(po["LIFNR"].astype(str)).sum()
    return str(val.idxmax()) if len(val) else None


def normalise(specs: Sequence[WhatIfSpec], db: Db | None = None) -> list[WhatIfSpec]:
    out = []
    for s in specs:
        spec = s.model_copy(update={"target": resolve_target(s.kind, s.target, db)})
        if spec not in out:
            out.append(spec)
    return out


def _duration(text: str, default: int) -> int:
    """Disruption duration only when phrased as a period ('for 3 weeks', 'lasting 10 days'), else the default."""
    m = re.search(r"(?:for|lasting|last|next|over)\s+(?:the\s+)?(?:next\s+)?(\d{1,3})\s*(day|week|month)s?",
                  text.lower())
    if not m:
        return default
    return max(1, min(365, int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]))


def _pct(text: str) -> float | None:
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|per ?cent)", text.lower())
    return float(m.group(1)) if m else None


def _has(text: str, words: Sequence[str]) -> bool:
    t = text.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(w)}s?(?![a-z])", t) for w in words)


def _chokepoints_in(places: sea_routes.Places) -> list[str]:
    """Named chokepoints minus those only mentioned as the bypass of another named one (e.g. 'rerouting via Cape')."""
    cfg = sea_routes.load()["chokepoints"]
    bypasses = {cfg[c].get("bypass_via") for c in places.chokepoints}
    return sorted(c for c in places.chokepoints if c not in bypasses)


def parse_whatif_rules(text: str, db: Db | None, rules: Rules) -> list[WhatIfSpec]:
    """Deterministic what-if parser: places by alias, kinds by keyword, % and duration by regex."""
    places = sea_routes.match_places(text)
    dur = _duration(text, int(_r(rules, "scenario.default_duration_days")))
    closure = _has(text, CLOSURE_WORDS)
    specs: list[WhatIfSpec] = []
    for cp in _chokepoints_in(places):
        kind = WhatIfKind.CHOKEPOINT_CLOSURE if closure else WhatIfKind.CHOKEPOINT_RISK
        specs.append(WhatIfSpec(kind=kind, target=cp, duration_days=dur))
    for port in sorted(places.indian_ports):
        kind = WhatIfKind.PORT_CLOSURE if closure else WhatIfKind.PORT_CONGESTION
        specs.append(WhatIfSpec(kind=kind, target=port, duration_days=dur))
    if _has(text, TARIFF_WORDS):
        pct = _pct(text) or float(_r(rules, "scenario.default_tariff_pct"))
        for c in sorted(places.countries) or ["ALL"]:
            specs.append(WhatIfSpec(kind=WhatIfKind.TARIFF_INCREASE, target=c, magnitude_pct=pct, duration_days=dur))
    if _has(text, EXPORT_WORDS):
        for c in sorted(places.countries):
            specs.append(WhatIfSpec(kind=WhatIfKind.ORIGIN_EXPORT_STOP, target=c, duration_days=dur))
    named = _match_vendor(text, db) if db is not None else []
    if db is not None and (_has(text, SUPPLIER_WORDS) or (named and _has(text, OFFLINE_WORDS))):
        vendors = named or ([k] if (k := key_supplier(db)) else [])
        for v in vendors[:1]:
            specs.append(WhatIfSpec(kind=WhatIfKind.SUPPLIER_OFFLINE, target=v, duration_days=dur))
    return normalise(specs, db)


WHATIF_PROMPT = """\
Convert a what-if question about a pharmaceutical supply disruption into JSON specs.
Allowed kinds: CHOKEPOINT_CLOSURE, CHOKEPOINT_RISK, PORT_CLOSURE, PORT_CONGESTION, TARIFF_INCREASE,
SUPPLIER_OFFLINE, ORIGIN_EXPORT_STOP.
Known chokepoints: {chokepoints}.
Known Indian ports: {ports}.
Origin country codes: {origins}.
Targets: a chokepoint or port name from the lists, an origin country code (TARIFF_INCREASE may use ALL),
or for SUPPLIER_OFFLINE the supplier name exactly as written in the question ("KEY" if unnamed).
magnitude_pct only for tariffs. duration_days only if stated. Do not invent anything not in the question.
Respond ONLY with: {{"specs": [{{"kind": "...", "target": "...", "magnitude_pct": 0, "duration_days": null}}]}}
Use an empty list if the question is not about a supply disruption."""


async def parse_whatif(text: str, settings: Settings, db: Db | None, rules: Rules) -> tuple[list[WhatIfSpec], bool, str | None]:
    """(specs, used_llm, note). LLM first when configured; the deterministic parser otherwise or on failure."""
    fallback_note = "OPENROUTER_API_KEY not configured"
    if settings.openrouter_api_key:
        cfg = sea_routes.load()
        prompt = WHATIF_PROMPT.format(chokepoints=", ".join(cfg["chokepoints"]), ports=", ".join(cfg["indian_ports"]),
                                      origins=", ".join(sea_routes.origins()))
        try:
            resp = await chat_completion([{"role": "system", "content": prompt}, {"role": "user", "content": text[:2000]}],
                                         settings, response_format={"type": "json_object"}, max_tokens=400)
            dur_default = int(_r(rules, "scenario.default_duration_days"))
            specs = []
            for raw in (extract_json(resp).get("specs") or [])[:6]:
                target = str(raw.get("target") or "")
                kind = WhatIfKind(str(raw.get("kind")))
                if kind is WhatIfKind.SUPPLIER_OFFLINE and target.upper() == "KEY" and db is not None:
                    target = key_supplier(db) or target
                specs.append(WhatIfSpec(kind=kind, target=target,
                                        magnitude_pct=float(raw.get("magnitude_pct") or 0.0),
                                        duration_days=int(raw.get("duration_days") or _duration(text, dur_default))))
            specs = normalise(specs, db)
            if specs:
                return specs, True, None
            fallback_note = "LLM returned no usable specs"
        except (OpenRouterError, ValueError, TypeError) as exc:
            fallback_note = f"LLM parse failed: {exc}"[:300]
    return parse_whatif_rules(text, db, rules), False, fallback_note


def specs_from_assessment(assessment: DisruptionAssessment, signal_text: str, rules: Rules,
                          db: Db | None = None, force_closure: bool | None = None) -> list[WhatIfSpec]:
    """Turn a sensed signal into what-if specs. Places come from the deterministic matcher, not the LLM."""
    text = " | ".join([signal_text, assessment.location or "", *(e.entity_name for e in assessment.affected_entities)])
    places = sea_routes.match_places(text)
    dur = _duration(signal_text, int(_r(rules, "scenario.default_duration_days")))
    sev_order = list(DisruptionSeverity)
    closure_sev = DisruptionSeverity(_r(rules, "sensing.closure_severity"))
    closure = (force_closure if force_closure is not None
               else sev_order.index(assessment.severity) >= sev_order.index(closure_sev) or _has(signal_text, CLOSURE_WORDS))
    et = assessment.event_type
    specs: list[WhatIfSpec] = []
    if et in (DisruptionCategory.MARITIME_SECURITY, DisruptionCategory.GEOPOLITICAL, DisruptionCategory.WEATHER,
              DisruptionCategory.PORT_CONGESTION, DisruptionCategory.OTHER):
        for cp in _chokepoints_in(places):
            specs.append(WhatIfSpec(kind=WhatIfKind.CHOKEPOINT_CLOSURE if closure else WhatIfKind.CHOKEPOINT_RISK,
                                    target=cp, duration_days=dur))
        for port in sorted(places.indian_ports):
            kind = WhatIfKind.PORT_CLOSURE if closure and et is not DisruptionCategory.PORT_CONGESTION \
                else WhatIfKind.PORT_CONGESTION
            if et in (DisruptionCategory.WEATHER, DisruptionCategory.PORT_CONGESTION) or not places.chokepoints:
                specs.append(WhatIfSpec(kind=kind, target=port, duration_days=dur))
    if et is DisruptionCategory.TARIFF or (et is DisruptionCategory.OTHER and _has(signal_text, TARIFF_WORDS)):
        pct = _pct(signal_text) or float(_r(rules, "scenario.default_tariff_pct"))
        for c in sorted(places.countries) or ["ALL"]:
            specs.append(WhatIfSpec(kind=WhatIfKind.TARIFF_INCREASE, target=c, magnitude_pct=pct, duration_days=dur))
    if _has(signal_text, EXPORT_WORDS) or (et is DisruptionCategory.SUPPLIER and places.countries):
        for c in sorted(places.countries):
            specs.append(WhatIfSpec(kind=WhatIfKind.ORIGIN_EXPORT_STOP, target=c, duration_days=dur))
    if et is DisruptionCategory.SUPPLIER and db is not None:
        for v in _match_vendor(text, db)[:1]:
            specs.append(WhatIfSpec(kind=WhatIfKind.SUPPLIER_OFFLINE, target=v, duration_days=dur))
    return normalise(specs, db)


# ---------------------------------------------------------------- state


@dataclass
class DisruptionState:
    lanes: sea_routes.LaneState
    tariffs: dict[str, float] = field(default_factory=dict)
    offline_suppliers: set[str] = field(default_factory=set)
    export_stops: set[str] = field(default_factory=set)

    @property
    def duration_days(self) -> int:
        return self.lanes.duration_days


def build_state(specs: Sequence[WhatIfSpec]) -> DisruptionState:
    st = DisruptionState(sea_routes.LaneState(duration_days=max((s.duration_days for s in specs), default=30)))
    for s in specs:
        k = s.kind
        if k is WhatIfKind.CHOKEPOINT_CLOSURE:
            st.lanes.blocked_chokepoints.add(s.target)
        elif k is WhatIfKind.CHOKEPOINT_RISK:
            st.lanes.risky_chokepoints.add(s.target)
        elif k is WhatIfKind.PORT_CLOSURE:
            st.lanes.blocked_ports.add(s.target)
        elif k is WhatIfKind.PORT_CONGESTION:
            st.lanes.congested_ports.add(s.target)
        elif k is WhatIfKind.TARIFF_INCREASE:
            st.tariffs[s.target] = max(st.tariffs.get(s.target, 0.0), s.magnitude_pct)
        elif k is WhatIfKind.SUPPLIER_OFFLINE:
            st.offline_suppliers.add(s.target)
        elif k is WhatIfKind.ORIGIN_EXPORT_STOP:
            st.export_stops.add(s.target)
    st.lanes.risky_chokepoints -= st.lanes.blocked_chokepoints
    return st


# ---------------------------------------------------------------- data


def origin_shares(db: Db, rules: Rules) -> dict[str, dict[str, dict[str, float]]]:
    """form_id -> api_id -> {origin: import share}, from each API's latest month (shares below the floor dropped)."""
    df = try_query(db, "SELECT fa.FORM_ID, o.API_ID, o.MONTH, o.COUNTRY, o.SHARE FROM FF_REF_FORM_API fa "
                       "JOIN FF_REF_API_ORIGIN o ON o.API_ID = fa.API_ID")
    if df is None or df.empty:
        return {}
    df["SHARE"] = pd.to_numeric(df["SHARE"], errors="coerce").fillna(0.0)
    df["MONTH"] = df["MONTH"].astype(str)
    df = df[df["MONTH"] == df.groupby("API_ID")["MONTH"].transform("max")]
    floor = float(_r(rules, "scenario.exposure_min_share"))
    out: dict[str, dict[str, dict[str, float]]] = {}
    for r in df[df["SHARE"] >= floor].itertuples():
        out.setdefault(str(r.FORM_ID), {}).setdefault(str(r.API_ID), {})[str(r.COUNTRY)] = float(r.SHARE)
    return out


def tariff_multipliers(state: DisruptionState, shares: dict[str, dict[str, dict[str, float]]]) -> dict[str, float]:
    """API cost multiplier per formulation implied by the tariffs (worst API, share-weighted) for the A1 re-check."""
    if not state.tariffs:
        return {}
    out = {}
    for form, apis in shares.items():
        uplift = max(sum(s * state.tariffs.get(o, state.tariffs.get("ALL", 0.0)) for o, s in by_origin.items())
                     for by_origin in apis.values())
        if uplift > 0:
            out[form] = round(1.0 + uplift / 100.0, 3)
    return out


@dataclass
class _Stock:
    material: str
    form_id: str
    name: str | None
    ved: str | None
    plant: str | None
    usable: float
    daily: float


def stocked(db: Db) -> list[_Stock]:
    df = try_query(db, "SELECT m.MATNR, m.MAKTX, m.FORM_ID, m.VED, d.WERKS, d.USABLE_QTY, d.AVG_DAILY_ISSUE_90D "
                       "FROM FF_MM_MARA m JOIN FF_V_DAYS_OF_COVER d ON d.MATNR = m.MATNR WHERE m.FORM_ID IS NOT NULL")
    if df is None or df.empty:
        return []
    df["U"] = pd.to_numeric(df["USABLE_QTY"], errors="coerce").fillna(0.0)
    df["D"] = pd.to_numeric(df["AVG_DAILY_ISSUE_90D"], errors="coerce").fillna(0.0)
    out = []
    for matnr, g in df.groupby("MATNR"):
        top = g.sort_values("U", ascending=False).iloc[0]
        out.append(_Stock(str(matnr), str(top["FORM_ID"]), top["MAKTX"], top["VED"], top["WERKS"],
                          float(g["U"].sum()), float(g["D"].sum())))
    return out


# ---------------------------------------------------------------- exposure


@dataclass
class _Exp:
    """Exposure numbers for one formulation, before strategies."""

    stock: _Stock
    lanes: list[LaneExposure]
    base_days: float
    doc: float
    e_absorb: float
    e_reroute: float | None
    e_loss: float
    loss_share: float
    tariff_pct: float
    sec_share: float
    lane_share: float
    consignment_value: float
    rate: float | None
    open_qty: float
    supplier_only: bool
    other_vendors: bool


def exposure(state: DisruptionState, db: Db, rules: Rules, as_of) -> list[_Exp]:
    shares = origin_shares(db, rules)
    k = sea_routes.consts_from_rules(rules)
    passthrough = float(_r(rules, "scenario.lead_time_passthrough"))
    lead = float(rules.value("weights", "exposure.lead_time_days"))
    requal = float(rules.value("weights", "exposure.requal_days"))
    outage = min(state.duration_days, requal)
    po = po_lines(db)
    open_po = po_lines(db, open_only=True)
    lane_cache: dict[str, tuple] = {}

    def lane(origin: str) -> tuple:
        if origin not in lane_cache:
            routes = sea_routes.routes_for(origin)
            touched = bool(routes) and state.lanes.touches(routes[0])
            opts = sea_routes.lane_options(origin, state.lanes, k) if touched else []
            absorb = sea_routes.absorb_option(opts)
            rr = sea_routes.best_reroute(opts)
            if absorb and rr and not (rr.extra_days < absorb.extra_days or absorb.security_risk or absorb.kind == "HOLD"):
                rr = None
            lane_cache[origin] = (touched, absorb, rr)
        return lane_cache[origin]

    def lane_exposure(origin: str, share: float) -> LaneExposure | None:
        touched, absorb, rr = lane(origin)
        loss = 1.0 if origin in state.export_stops else 0.0
        tariff = state.tariffs.get(origin, state.tariffs.get("ALL", 0.0))
        if not (touched or loss or tariff):
            return None
        return LaneExposure(
            origin=origin, share=round(share, 4),
            absorb_delay_days=float(max(0, absorb.extra_days)) if absorb else 0.0,
            reroute_delay_days=float(max(0, rr.extra_days)) if rr else None,
            reroute_label=rr.label if rr else None,
            security_risk=bool(absorb and absorb.security_risk), supply_loss=loss, tariff_pct=tariff)

    out: list[_Exp] = []
    for s in stocked(db):
        if s.daily <= 0:
            continue
        # every API is needed to make the formulation, so each metric takes its worst API
        per_api = [[le for o, sh in by_origin.items() if (le := lane_exposure(o, sh))]
                   for by_origin in shares.get(s.form_id, {}).values()]
        per_api = [ls for ls in per_api if ls]

        def worst(fn) -> float:
            return max((fn(ls) for ls in per_api), default=0.0)

        lanes_by_origin: dict[str, LaneExposure] = {}
        for ls in per_api:
            for le in ls:
                if le.origin not in lanes_by_origin or le.share > lanes_by_origin[le.origin].share:
                    lanes_by_origin[le.origin] = le
        lanes = sorted(lanes_by_origin.values(), key=lambda le: (-le.share, le.origin))
        mine = po[po["MATNR"].astype(str) == s.material]
        vend_qty = mine.groupby(mine["LIFNR"].astype(str))["MENGE"].sum()
        vend_share = (vend_qty / vend_qty.sum()).to_dict() if vend_qty.sum() > 0 else {}
        vendor_loss = sum(vend_share.get(v, 0.0) for v in state.offline_suppliers)
        e_abs = passthrough * worst(lambda ls: sum(l.share * l.absorb_delay_days for l in ls))
        has_rr = any(l.reroute_delay_days is not None for l in lanes)
        e_rr = passthrough * worst(lambda ls: sum(
            l.share * (l.reroute_delay_days if l.reroute_delay_days is not None else l.absorb_delay_days)
            for l in ls)) if has_rr else None
        loss_share = min(1.0, worst(lambda ls: sum(l.share * l.supply_loss for l in ls)) + vendor_loss)
        tariff_pct = worst(lambda ls: sum(l.share * l.tariff_pct for l in ls))
        sec_share = min(1.0, worst(lambda ls: sum(l.share for l in ls if l.security_risk)))
        if e_abs <= 0 and loss_share <= 0 and tariff_pct <= 0 and sec_share <= 0:
            continue
        mine_open = open_po[open_po["MATNR"].astype(str) == s.material]
        open_qty = float(mine_open["MENGE"].sum())
        first_eta = min(mine_open["EINDT"]) if len(mine_open) else None
        base = min(lead, max(0.0, (first_eta - as_of).days)) if first_eta else lead
        rate = None
        if len(mine) and mine["UNIT_NET"].notna().any():
            rate = float(mine.dropna(subset=["UNIT_NET"]).sort_values("BEDAT").iloc[-1]["UNIT_NET"])
        value = float((mine_open["MENGE"] * mine_open["UNIT_NET"].fillna(rate or 0.0)).sum()) if open_qty else \
            s.daily * lead * (rate or 0.0)
        out.append(_Exp(
            stock=s, lanes=lanes, base_days=base, doc=s.usable / s.daily, e_absorb=e_abs, e_reroute=e_rr,
            e_loss=loss_share * outage, loss_share=loss_share, tariff_pct=tariff_pct, sec_share=sec_share,
            lane_share=min(1.0, worst(lambda ls: sum(l.share for l in ls if l.absorb_delay_days > 0 or l.security_risk))),
            consignment_value=value, rate=rate, open_qty=open_qty,
            supplier_only=bool(vendor_loss) and not lanes, other_vendors=len(vend_share) > 1,
        ))
    return out


# ---------------------------------------------------------------- strategies


def _criticality(ved: str | None, rules: Rules) -> float:
    try:
        return float(rules.value("weights", f"criticality.{(ved or 'E').strip().upper()}"))
    except KeyError:
        return float(rules.value("weights", "criticality.E"))


def evaluate(x: _Exp, margin_band: SignalBand | None, rules: Rules) -> FormulationImpact:
    """Cost / time / risk of each strategy for one exposed formulation, and the lowest-score pick."""
    d, doc, base = x.stock.daily, x.doc, x.base_days
    requal = float(rules.value("weights", "exposure.requal_days"))
    crit = _criticality(x.stock.ved, rules)
    full_days = float(_r(rules, "scenario.stockout_days_full_risk"))
    sec_w = float(_r(rules, "scenario.security_risk_weight"))
    e_tariff = float(_r(rules, f"scenario.tariff_exit_loss_share.{margin_band.value}")) * requal \
        if margin_band is not None and x.tariff_pct > 0 else 0.0
    e_total = x.e_absorb + x.e_loss + e_tariff
    rate = x.rate or 0.0

    def short(e: float, cap_days: float | None = None) -> int:
        end = base + e if cap_days is None else min(base + e, cap_days)
        return int(math.ceil(d * max(0.0, end - doc)))

    def risk(units: int, sec: float) -> float:
        return round(min(1.0, crit * (units / d) / full_days + sec), 4)

    short_abs = short(e_total)
    evals: list[StrategyEval] = [StrategyEval(
        strategy=StrategyType.ABSORB, feasible=True, cost_inr=_money(0), time_to_effect_days=0,
        stockout_units=short_abs, residual_risk=risk(short_abs, sec_w * x.sec_share),
        note="no intervention: wait out the delay" + (" on a route with a security alert" if x.sec_share else ""))]

    if x.e_reroute is not None:
        rr_days = min(max((l.reroute_delay_days or 0) for l in x.lanes),
                      sum(l.share * (l.reroute_delay_days or 0) for l in x.lanes if l.reroute_delay_days is not None))
        cost = x.consignment_value * float(_r(rules, "logistics.reroute_surcharge_pct_per_day")) * rr_days
        u = short(x.e_reroute + x.e_loss + e_tariff)
        sec_left = sum(l.share for l in x.lanes if l.security_risk and l.reroute_delay_days is None)
        labels = sorted({l.reroute_label for l in x.lanes if l.reroute_label})
        evals.append(StrategyEval(strategy=StrategyType.REROUTE, feasible=True, cost_inr=_money(cost),
                                  time_to_effect_days=1, stockout_units=u, residual_risk=risk(u, sec_w * sec_left),
                                  note="; ".join(labels)))
    else:
        evals.append(StrategyEval(strategy=StrategyType.REROUTE, feasible=False, cost_inr=_money(0),
                                  time_to_effect_days=0, stockout_units=short_abs, residual_risk=1.0,
                                  note="no faster or safer lane mapped"))

    air_ok = x.lane_share > 0 and x.consignment_value <= float(_r(rules, "logistics.air_max_consignment_value_inr"))
    u = short(x.e_loss + e_tariff)
    evals.append(StrategyEval(
        strategy=StrategyType.AIR_EXPEDITE, feasible=air_ok,
        cost_inr=_money(x.consignment_value * float(_r(rules, "logistics.air_freight_pct_of_value")) * x.lane_share
                        if air_ok else 0),
        time_to_effect_days=float(_r(rules, "scenario.air_transit_days")) if air_ok else 0,
        stockout_units=u if air_ok else short_abs, residual_risk=risk(u, 0.0) if air_ok else 1.0,
        note=None if air_ok else ("consignment above the air-freight value cap" if x.lane_share else "no lane delay to fly around")))

    lead_b = float(_r(rules, "scenario.buffer_lead_days"))
    avail = float(_r(rules, "scenario.domestic_stock_availability"))
    buf_ok = short_abs > 0 and rate > 0
    u = min(short_abs, int(math.ceil(d * max(0.0, lead_b - doc) + (1 - avail) * short_abs)))
    evals.append(StrategyEval(
        strategy=StrategyType.BUFFER_STOCK, feasible=buf_ok,
        cost_inr=_money(short_abs * rate * float(_r(rules, "scenario.buffer_spot_premium")) if buf_ok else 0),
        time_to_effect_days=lead_b if buf_ok else 0, stockout_units=u if buf_ok else short_abs,
        residual_risk=risk(u, sec_w * x.sec_share * (1 - avail)) if buf_ok else 1.0,
        note=f"emergency order of {short_abs} units from domestic distributors" if buf_ok else "no projected shortfall"))

    t_alt = lead_b if (x.supplier_only and x.other_vendors) else requal
    alt_ok = short_abs > 0 and rate > 0 and t_alt < base + e_total
    q_alt = int(math.ceil(d * max(0.0, base + e_total - t_alt)))
    u = short(e_total, cap_days=t_alt)
    evals.append(StrategyEval(
        strategy=StrategyType.ALT_SOURCING, feasible=alt_ok,
        cost_inr=_money(q_alt * rate * float(rules.value("weights", "resilience.alt_price_premium")) if alt_ok else 0),
        time_to_effect_days=t_alt if alt_ok else 0, stockout_units=u if alt_ok else short_abs,
        residual_risk=risk(u, 0.0) if alt_ok else 1.0,
        note=("switch to another qualified vendor" if x.supplier_only else
              "alternate manufacturers import the same API; needs an alternative-origin or therapeutic source "
              "(requalification + clinical review)") if alt_ok else "requalification is slower than the disruption"))

    scored = [_score(e, rules) for e in evals]
    feasible = [e for e in scored if e.feasible]
    best = min(feasible, key=lambda e: (e.score, STRATEGY_ORDER.index(e.strategy)))
    return FormulationImpact(
        formulation_id=x.stock.form_id, material=x.stock.material, name=x.stock.name, ved=x.stock.ved,
        daily_consumption=round(d, 4), usable_stock=round(x.stock.usable, 4), days_of_cover=round(doc, 1),
        open_po_qty=x.open_qty, unit_rate_inr=_money(rate) if x.rate is not None else None, lanes=x.lanes,
        exposure_days_absorb=round(e_total, 2), baseline_stockout_units=short(0.0),
        exposure_days_reroute=round((x.e_reroute if x.e_reroute is not None else x.e_absorb) + x.e_loss + e_tariff, 2),
        supply_loss_share=round(x.loss_share, 4), tariff_uplift_pct=round(x.tariff_pct, 2),
        margin_band_after=margin_band, strategies=scored, recommended=best.strategy)


def _score(e: StrategyEval, rules: Rules) -> StrategyEval:
    if not e.feasible:
        return e
    c, t = float(e.cost_inr), e.time_to_effect_days
    cref, tref = float(_r(rules, "scenario.cost_ref_inr")), float(_r(rules, "scenario.time_ref_days"))
    s = (float(_r(rules, "scenario.weights.cost")) * c / (c + cref)
         + float(_r(rules, "scenario.weights.time")) * t / (t + tref)
         + float(_r(rules, "scenario.weights.risk")) * e.residual_risk)
    return e.model_copy(update={"score": round(s, 4)})


def roll_up(impacts: Sequence[FormulationImpact]) -> tuple[list[PlanOption], str | None]:
    """Plan options: each pure strategy (ABSORB where infeasible) and the recommended per-formulation mix."""
    if not impacts:
        return [], None
    options = []

    def option(option_id: str, label: str, picks: list[StrategyEval], covered: int) -> PlanOption:
        return PlanOption(
            option_id=option_id, label=label, cost_inr=_money(sum(float(p.cost_inr) for p in picks)),
            time_to_effect_days=max(p.time_to_effect_days for p in picks),
            stockout_units=sum(p.stockout_units for p in picks),
            residual_risk=round(sum(p.residual_risk for p in picks) / len(picks), 4), formulations_covered=covered,
            score=round(sum(p.score or 0.0 for p in picks) / len(picks), 4))

    for st in STRATEGY_ORDER:
        picks, covered = [], 0
        for i in impacts:
            by = {e.strategy: e for e in i.strategies}
            e = by[st] if by[st].feasible else by[StrategyType.ABSORB]
            covered += int(by[st].feasible)
            picks.append(e)
        options.append(option(st.value, st.value.replace("_", " ").title(), picks, covered))
    mix = [next(e for e in i.strategies if e.strategy is i.recommended) for i in impacts]
    counts = {st: sum(1 for i in impacts if i.recommended is st) for st in STRATEGY_ORDER}
    label = "Recommended mix (medicines per strategy: " + ", ".join(
        f"{st.value.replace('_', ' ').lower()} {n}" for st, n in counts.items() if n) + ")"
    options.append(option("RECOMMENDED_MIX", label, mix, len(impacts)))
    best = min(options, key=lambda o: (o.score, o.option_id != "RECOMMENDED_MIX"))
    return options, best.option_id


# ---------------------------------------------------------------- run / persist


def run(specs: Sequence[WhatIfSpec], ctx: Any, margin_bands: dict[str, SignalBand] | None = None,
        signal_id: str | None = None) -> ScenarioPlan:
    """Simulate the what-ifs against current stock and return the ranked plan (does not write)."""
    c = resolve_ctx(ctx, "wf")
    specs = normalise(specs, c.db)
    state = build_state(specs)
    as_of = c.run.as_of or as_of_date(c.db)
    bands = margin_bands or {}
    impacts = [evaluate(x, bands.get(x.stock.form_id), c.rules) for x in exposure(state, c.db, c.rules, as_of)]
    impacts.sort(key=lambda i: (-max(e.residual_risk for e in i.strategies if e.strategy is StrategyType.ABSORB),
                                i.formulation_id))
    options, recommended = roll_up(impacts)
    return ScenarioPlan(plan_id=f"wf-{uuid.uuid4().hex[:10]}", run_id=c.run.run_id, signal_id=signal_id,
                        specs=specs, as_of=as_of, impacts=impacts, options=options,
                        recommended_option=recommended, data_sources=SOURCES)


def save(plan: ScenarioPlan, db: Db) -> None:
    rec = next((o for o in plan.options if o.option_id == plan.recommended_option), None)
    upsert(db, "FF_AG_WHATIF", ("PLAN_ID",), {
        "PLAN_ID": plan.plan_id, "RUN_ID": plan.run_id, "SIGNAL_ID": plan.signal_id,
        "SPECS_JSON": json.dumps([s.model_dump(mode="json") for s in plan.specs]),
        "RECOMMENDED_OPTION": plan.recommended_option, "N_EXPOSED": len(plan.impacts),
        "STOCKOUT_UNITS": float(rec.stockout_units) if rec else 0.0, "USED_LLM": int(plan.narrative_used_llm),
        "NARRATIVE": plan.narrative, "PLAN_JSON": plan.model_dump_json(), "CREATED_AT": db_ts(plan.created_at),
    })


def load_plan(plan_id: str, db: Db) -> ScenarioPlan | None:
    df = try_query(db, "SELECT PLAN_JSON FROM FF_AG_WHATIF WHERE PLAN_ID = ?", (plan_id,))
    return None if df is None or df.empty else ScenarioPlan.model_validate_json(str(df.iat[0, 0]))


def recent_plans(db: Db, limit: int = 20) -> list[dict[str, Any]]:
    df = try_query(db, "SELECT PLAN_ID, RUN_ID, SIGNAL_ID, SPECS_JSON, RECOMMENDED_OPTION, N_EXPOSED, STOCKOUT_UNITS, "
                       f"USED_LLM, CREATED_AT FROM FF_AG_WHATIF ORDER BY CREATED_AT DESC LIMIT {int(limit)}")
    return [] if df is None or df.empty else df.where(df.notna(), None).to_dict(orient="records")


# ---------------------------------------------------------------- narrative


def fact_sheet(plan: ScenarioPlan) -> dict[str, Any]:
    absorb = {i.formulation_id: next(e for e in i.strategies if e.strategy is StrategyType.ABSORB) for i in plan.impacts}
    return {
        "what_if": [{"kind": s.kind.value, "target": s.target, "magnitude_pct": s.magnitude_pct,
                     "duration_days": s.duration_days} for s in plan.specs],
        "exposed_formulations": len(plan.impacts),
        "stock_out_if_absorbed": sum(1 for a in absorb.values() if a.stockout_units > 0),
        "baseline_stockout_units": sum(i.baseline_stockout_units for i in plan.impacts),
        "top_risks": [{"medicine": i.name or i.formulation_id, "ved": i.ved, "days_of_cover": i.days_of_cover,
                       "delay_days_if_absorbed": i.exposure_days_absorb,
                       "stockout_units_if_absorbed": absorb[i.formulation_id].stockout_units,
                       "recommended": i.recommended.value if i.recommended else None} for i in plan.impacts[:5]],
        "options": [{"option": o.label, "cost_inr": round(float(o.cost_inr), 2), "stockout_units": o.stockout_units,
                     "time_to_effect_days": o.time_to_effect_days, "residual_risk": o.residual_risk}
                    for o in plan.options],
        "recommended_option": plan.recommended_option,
    }


def template_narrative(plan: ScenarioPlan) -> str:
    if not plan.impacts:
        return "No stocked formulation depends on the disrupted lanes, ports, origins or suppliers. Monitor only."
    f = fact_sheet(plan)
    rec = next(o for o in plan.options if o.option_id == plan.recommended_option)
    absorb = next(o for o in plan.options if o.option_id == "ABSORB")
    what = "; ".join(f"{w['kind'].replace('_', ' ').lower()} at {w['target']}"
                     + (f" (+{w['magnitude_pct']:g}%)" if w["magnitude_pct"] else "") for w in f["what_if"])
    top = ", ".join(f"{t['medicine']} (VED {t['ved'] or '-'}, {t['days_of_cover']} days of cover, "
                    f"+{t['delay_days_if_absorbed']} days)" for t in f["top_risks"][:3])
    return (f"What-if: {what}. {f['exposed_formulations']} stocked formulations are exposed; "
            f"{f['stock_out_if_absorbed']} would stock out if the disruption is simply absorbed "
            f"({absorb.stockout_units} units short, of which {f['baseline_stockout_units']} would be short even without "
            f"it). {rec.label}: about INR {float(rec.cost_inr):.0f} incremental cost, "
            f"{rec.stockout_units} units short, up to {rec.time_to_effect_days:g} days to take effect. "
            f"Highest-risk items: {top}. These are planning estimates from rules/disruption.yaml for human review.")


async def narrate(plan: ScenarioPlan, settings: Settings) -> ScenarioPlan:
    """LLM-written trade-off brief grounded in the fact sheet; the template when unavailable or ungrounded."""
    template = template_narrative(plan)
    if not settings.openrouter_api_key or not plan.impacts:
        return plan.model_copy(update={"narrative": template, "narrative_used_llm": False})
    sheet = fact_sheet(plan)
    prompt = ("You brief a hospital Chief Pharmacist on a supply-disruption what-if. Write 4-6 plain sentences: what "
              "is disrupted, how many medicines are exposed, the trade-off between the options (cost, time, stock-out "
              "units) and why the recommended option is preferred. Use ONLY numbers that appear in the fact sheet, "
              "written without thousands separators. Frame everything as planning estimates for human review; never "
              "state a manufacturer's, carrier's or government's intent.\n\nFact sheet:\n"
              + json.dumps(sheet, indent=1, default=str))
    try:
        resp = await chat_completion([{"role": "user", "content": prompt}], settings, max_tokens=400, temperature=0.2)
        text = extract_content(resp).strip()
        ok, err = verify_numbers(re.sub(r"(?<=\d),(?=\d)", "", text), sheet)
        if ok and text:
            return plan.model_copy(update={"narrative": text, "narrative_used_llm": True})
        log.warning("scenario narrative discarded: %s", err)
    except OpenRouterError as exc:
        log.warning("scenario narrative LLM call failed: %s", exc)
    return plan.model_copy(update={"narrative": template, "narrative_used_llm": False})
