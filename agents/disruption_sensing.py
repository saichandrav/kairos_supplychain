"""Disruption Sensing Agent: classifies and assesses incoming supply-chain signals via OpenRouter.

The agent receives raw external signals (maritime alerts, weather, tariff, port congestion,
geopolitical news) and uses an LLM to produce a structured assessment. It cross-references
the places named in the signal against pharmaceutical sea lanes (rules/sea_routes.yaml) and the
hospital's API origin dependencies (FF_REF_API_ORIGIN / FF_REF_FORM_API) to find exposed formulations.

Guardrails:
- Never fabricates missing facts; represents unavailable info as null/unknown.
- Clearly separates source-reported facts from inferred impacts.
- Place matching is deterministic (whole-phrase aliases), so routes are found even without the LLM.
- Ambiguous signals (confidence below rules/disruption.yaml sensing.escalate_confidence_below, or no
  identifiable place) are escalated to a human analyst instead of driving autonomous planning.
- All output is validated via Pydantic before downstream use.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from agents import sea_routes
from agents.common import db_ts, try_query, upsert
from agents.contracts import (
    AffectedEntity,
    AffectedSeaRoute,
    DisruptionAssessment,
    DisruptionCategory,
    DisruptionSeverity,
    DisruptionSignalIn,
)
from agents.ctx import Db, Settings, get_db, load_settings
from agents.rules import Rules, load_rules
from services.openrouter import (
    OpenRouterError,
    chat_completion,
    extract_json,
)

log = logging.getLogger(__name__)

AGENT = "DX1_SENSING"
SEVERITY_ORDER = {s: i for i, s in enumerate(DisruptionSeverity)}


def load_sea_routes() -> dict[str, Any]:
    """rules/sea_routes.yaml (cached)."""
    return sea_routes.load()


def _reroute_delay(route: dict[str, Any], chokepoint: str) -> int | None:
    info = sea_routes.load()["chokepoints"].get(chokepoint, {})
    if route.get("reroute_via") and info.get("bypass_via") == route["reroute_via"] and route.get("transit_days_rerouted"):
        return int(route["transit_days_rerouted"]) - int(route["transit_days_normal"])
    return int(info["bypass_extra_days"]) if info.get("bypass_extra_days") else None


def find_affected_routes(
    location_text: str,
    affected_entities: list[AffectedEntity],
) -> list[dict[str, Any]]:
    """Routes whose chokepoints, ports or origin country are named in the location text or entities."""
    text = " | ".join([location_text or "", *(e.entity_name for e in affected_entities)])
    places = sea_routes.match_places(text)
    if not places:
        return []
    matched: list[dict[str, Any]] = []
    for route in sea_routes.load()["routes"]:
        cp = next((c for c in route.get("chokepoints", []) if c in places.chokepoints), None)
        port_hit = bool(set(route.get("destination_ports", [])) & places.indian_ports
                        or set(route.get("origin_ports", [])) & places.origin_ports)
        if not (cp or port_hit or route["origin"] in places.countries):
            continue
        hit = dict(route)
        hit["disrupted_chokepoint"] = cp
        if cp:
            hit["delay_days"] = _reroute_delay(route, cp)
            if hit["delay_days"] is not None and not route.get("transit_days_rerouted"):
                hit["transit_days_rerouted"] = int(route["transit_days_normal"]) + hit["delay_days"]
                hit["reroute_via"] = sea_routes.load()["chokepoints"][cp].get("bypass_via")
        matched.append(hit)
    return matched


def get_formulations_by_origin(origin_country: str, db: Db) -> list[str]:
    """Formulations whose API gets more than 5% of its imports from the origin country."""
    sql = """
    SELECT DISTINCT fa.FORM_ID
    FROM FF_REF_FORM_API fa
    JOIN FF_REF_API_ORIGIN o ON o.API_ID = fa.API_ID
    WHERE o.COUNTRY = ? AND CAST(o.SHARE AS REAL) > 0.05
    ORDER BY fa.FORM_ID
    """
    if db.backend == "hana":
        sql = sql.replace("CAST(o.SHARE AS REAL)", "o.SHARE")
    df = try_query(db, sql, (origin_country,))
    if df is None or df.empty:
        return []
    return df["FORM_ID"].tolist()


def _build_sea_route_context(
    affected_routes: list[dict[str, Any]], db: Db | None
) -> tuple[list[AffectedSeaRoute], str]:
    """Convert matched routes to contract objects, query DB for affected formulations."""
    route_models: list[AffectedSeaRoute] = []
    context_lines: list[str] = []
    forms_by_origin: dict[str, list[str]] = {}

    for r in affected_routes:
        origin = r.get("origin", "??")
        if origin not in forms_by_origin:
            forms_by_origin[origin] = get_formulations_by_origin(origin, db) if db is not None else []
        formulations = forms_by_origin[origin]

        delay = r.get("delay_days")
        route_models.append(AffectedSeaRoute(
            origin=origin,
            origin_name=r.get("origin_name", origin),
            origin_ports=r.get("origin_ports", []),
            destination_ports=r.get("destination_ports", []),
            chokepoints=r.get("chokepoints", []),
            disrupted_chokepoint=r.get("disrupted_chokepoint"),
            transit_days_normal=r.get("transit_days_normal"),
            transit_days_rerouted=r.get("transit_days_rerouted"),
            reroute_via=r.get("reroute_via"),
            delay_days=delay,
            affected_formulations=formulations[:50],
        ))

        line = f"- {r.get('origin_name', origin)} ({origin}): "
        line += f"{', '.join(r.get('origin_ports', [])[:3])} → {', '.join(r.get('destination_ports', [])[:3])}"
        if r.get("disrupted_chokepoint"):
            line += f" [DISRUPTED at {r['disrupted_chokepoint']}]"
        if delay:
            line += f" (+{delay}d reroute via {r.get('reroute_via', 'alternate')})"
        if formulations:
            line += f" | {len(formulations)} formulation(s) exposed"
        context_lines.append(line)

    return route_models, "\n".join(context_lines)


SYSTEM_PROMPT = """\
You are a pharmaceutical supply-chain disruption analyst for an Indian tertiary hospital.

Your task: classify and assess a raw external signal for potential supply-chain impact.
You will receive the signal text plus a SEA ROUTE CONTEXT section showing which pharmaceutical
import routes and chokepoints are affected, based on the hospital's actual API (Active Pharmaceutical
Ingredient) supply dependencies.

RULES:
1. Classify into exactly one category: MARITIME_SECURITY, WEATHER, TARIFF, PORT_CONGESTION, GEOPOLITICAL, SUPPLIER, or OTHER.
2. Do NOT invent facts. If information is unavailable, use null.
3. Clearly distinguish source-reported facts from your inferred potential impacts.
4. When sea routes are affected, factor the transit delay and number of exposed formulations into your severity assessment.
5. Severity scale:
   - LOW: minimal supply-chain impact expected
   - MEDIUM: localized or short-term disruption possible
   - HIGH: significant disruption to pharmaceutical supply likely (e.g. attacks or security alerts on a route that stays open)
   - CRITICAL: immediate threat to essential medicine availability (e.g. a chokepoint or port closed to traffic)
6. Set requires_human_review=true for HIGH/CRITICAL severity, ambiguous signals, conflicting information, or insufficient evidence.
7. confidence is 0.0–1.0: your assessment confidence given the available evidence.

Respond with ONLY a JSON object (no markdown, no explanation) matching this schema:
{
  "event_type": "<category>",
  "summary": "<1-2 sentence summary>",
  "description": "<detailed description or null>",
  "severity": "<LOW|MEDIUM|HIGH|CRITICAL>",
  "location": "<geographic location or null>",
  "event_time": "<ISO timestamp or descriptive time or null>",
  "affected_entities": [{"entity_type": "<port|supplier|route|country|api_ingredient|chokepoint>", "entity_name": "<name>", "confidence": 0.0-1.0}],
  "potential_impact": "<supply-chain impact assessment incorporating sea route delays and affected formulations>",
  "confidence": 0.0-1.0,
  "evidence": ["<source-reported fact 1>", ...],
  "uncertainties": ["<what is unknown or conflicting>", ...],
  "recommended_actions": ["<suggested next step>", ...],
  "requires_human_review": true/false,
  "rationale": "<why this severity and classification>"
}"""


def _build_user_prompt(
    signal: DisruptionSignalIn,
    sea_route_context: str,
) -> str:
    parts = [
        f"Signal ID: {signal.signal_id}",
        f"Source: {signal.source}",
        f"Received: {signal.received_at.isoformat()}",
    ]
    if signal.source_url:
        parts.append(f"Source URL: {signal.source_url}")
    if signal.context:
        parts.append(f"Additional context: {json.dumps(signal.context, default=str)}")

    parts.append(f"\nSignal text:\n{signal.raw_text}")

    if sea_route_context:
        parts.append(f"\n--- SEA ROUTE CONTEXT (from hospital supply data) ---\n{sea_route_context}")
    else:
        parts.append("\n--- SEA ROUTE CONTEXT ---\nNo matching sea routes identified for this signal location.")

    return "\n".join(parts)


def _parse_assessment(
    data: dict[str, Any],
    signal: DisruptionSignalIn,
    model_id: str | None,
    affected_routes: list[AffectedSeaRoute],
) -> DisruptionAssessment:
    """Validate LLM output into a DisruptionAssessment, falling back for invalid values."""
    try:
        event_type = DisruptionCategory(data.get("event_type", "OTHER"))
    except ValueError:
        event_type = DisruptionCategory.OTHER

    try:
        severity = DisruptionSeverity(data.get("severity", "MEDIUM"))
    except ValueError:
        severity = DisruptionSeverity.MEDIUM

    affected = []
    for ent in data.get("affected_entities") or []:
        if isinstance(ent, dict) and ent.get("entity_type") and ent.get("entity_name"):
            conf = ent.get("confidence", 0.5)
            if not isinstance(conf, (int, float)) or conf < 0 or conf > 1:
                conf = 0.5
            affected.append(AffectedEntity(
                entity_type=str(ent["entity_type"])[:50],
                entity_name=str(ent["entity_name"])[:200],
                confidence=conf,
            ))

    confidence = data.get("confidence", 0.5)
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        confidence = 0.5

    requires_human = data.get("requires_human_review", True)
    if severity in (DisruptionSeverity.HIGH, DisruptionSeverity.CRITICAL):
        requires_human = True

    all_formulations: set[str] = set()
    for r in affected_routes:
        all_formulations.update(r.affected_formulations)

    return DisruptionAssessment(
        signal_id=signal.signal_id,
        event_type=event_type,
        summary=str(data.get("summary") or "Signal received — classification uncertain")[:500],
        description=(str(data["description"])[:2000] if data.get("description") else None),
        severity=severity,
        location=(str(data["location"])[:200] if data.get("location") else None),
        event_time=(str(data["event_time"])[:100] if data.get("event_time") else None),
        source=signal.source,
        affected_entities=affected,
        potential_impact=(str(data["potential_impact"])[:1000] if data.get("potential_impact") else None),
        confidence=confidence,
        evidence=[str(e)[:500] for e in (data.get("evidence") or []) if e][:10],
        uncertainties=[str(u)[:500] for u in (data.get("uncertainties") or []) if u][:10],
        recommended_actions=[str(a)[:500] for a in (data.get("recommended_actions") or []) if a][:10],
        requires_human_review=bool(requires_human),
        rationale=(str(data["rationale"])[:500] if data.get("rationale") else None),
        model_id=model_id,
        affected_sea_routes=affected_routes,
        total_affected_formulations=len(all_formulations),
    )


def _fallback_assessment(
    signal: DisruptionSignalIn,
    reason: str,
    affected_routes: list[AffectedSeaRoute] | None = None,
) -> DisruptionAssessment:
    """Deterministic fallback when LLM is unavailable or fails."""
    routes = affected_routes or []
    all_forms: set[str] = set()
    for r in routes:
        all_forms.update(r.affected_formulations)

    return DisruptionAssessment(
        signal_id=signal.signal_id,
        event_type=DisruptionCategory.OTHER,
        summary=f"Automated classification unavailable — {reason}",
        severity=DisruptionSeverity.MEDIUM,
        source=signal.source,
        confidence=0.0,
        uncertainties=[f"LLM assessment failed: {reason}"],
        requires_human_review=True,
        rationale=f"Fallback: {reason}",
        affected_sea_routes=routes,
        total_affected_formulations=len(all_forms),
    )


def escalate(assessment: DisruptionAssessment, signal_text: str, rules: Rules) -> DisruptionAssessment:
    """Flag ambiguous signals for a human analyst: low confidence, or no identifiable place."""
    thr = float(rules.value("disruption", "sensing.escalate_confidence_below"))
    located = bool(sea_routes.match_places(" | ".join(
        [signal_text, assessment.location or "", *(e.entity_name for e in assessment.affected_entities)])))
    reasons = []
    if assessment.confidence < thr:
        reasons.append(f"confidence {assessment.confidence:.2f} is below the {thr:.2f} escalation threshold")
    if not located:
        reasons.append("no mapped chokepoint, port or origin country could be identified")
    return assessment.model_copy(update={
        "escalated": bool(reasons),
        "escalation_reason": "; ".join(reasons) or None,
        "requires_human_review": assessment.requires_human_review or bool(reasons),
    })


def is_actionable(assessment: DisruptionAssessment, rules: Rules) -> bool:
    """MEDIUM and above (per rules) start scenario planning; below that the signal is only monitored."""
    floor = DisruptionSeverity(rules.value("disruption", "sensing.plan_min_severity"))
    return SEVERITY_ORDER[assessment.severity] >= SEVERITY_ORDER[floor]


async def assess(
    signal: DisruptionSignalIn,
    settings: Settings | None = None,
    db: Db | None = None,
    rules: Rules | None = None,
) -> DisruptionAssessment:
    """Classify and assess a disruption signal using OpenRouter, enriched with sea route data."""
    s = settings or load_settings()
    rules = rules or load_rules()

    preliminary_entities = [
        AffectedEntity(entity_type="location", entity_name=signal.raw_text[:500], confidence=0.5)
    ]
    matched_routes = find_affected_routes(signal.raw_text, preliminary_entities)

    route_db = db
    if route_db is None:
        try:
            route_db = get_db(s)
        except Exception:
            route_db = None

    affected_route_models, sea_route_text = _build_sea_route_context(matched_routes, route_db)

    if not s.openrouter_api_key:
        log.warning("OPENROUTER_API_KEY not set — returning fallback assessment")
        fb = _fallback_assessment(signal, "OPENROUTER_API_KEY not configured", affected_route_models)
        return escalate(fb, signal.raw_text, rules)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(signal, sea_route_text)},
    ]

    model_id = s.openrouter_model or "openai/gpt-4o-mini"
    try:
        response = await chat_completion(
            messages, s,
            response_format={"type": "json_object"},
            max_tokens=1024,
            temperature=0.2,
        )
        data = extract_json(response)
        assessment = _parse_assessment(data, signal, model_id, affected_route_models)
        log.info(
            "Disruption assessment: signal=%s type=%s severity=%s confidence=%.2f routes=%d formulations=%d",
            signal.signal_id, assessment.event_type.value, assessment.severity.value,
            assessment.confidence, len(assessment.affected_sea_routes),
            assessment.total_affected_formulations,
        )
        return escalate(assessment, signal.raw_text, rules)

    except OpenRouterError as exc:
        log.error("OpenRouter call failed for signal %s: %s", signal.signal_id, exc)
        return escalate(_fallback_assessment(signal, str(exc), affected_route_models), signal.raw_text, rules)
    except Exception as exc:
        log.error("Unexpected error assessing signal %s: %s", signal.signal_id, exc)
        return escalate(_fallback_assessment(signal, "internal error", affected_route_models), signal.raw_text, rules)


# ---------------------------------------------------------------- persistence


def record(signal: DisruptionSignalIn, assessment: DisruptionAssessment, status: str, db: Db,
           run_id: str | None = None) -> None:
    """Upsert the assessed signal → FF_AG_DISRUPTION."""
    upsert(db, "FF_AG_DISRUPTION", ("SIGNAL_ID",), {
        "SIGNAL_ID": signal.signal_id,
        "RUN_ID": run_id,
        "RECEIVED_AT": db_ts(signal.received_at),
        "SOURCE": signal.source[:120],
        "SOURCE_URL": signal.source_url[:500] if signal.source_url else None,
        "EVENT_TYPE": assessment.event_type.value,
        "SEVERITY": assessment.severity.value,
        "CONFIDENCE": float(assessment.confidence),
        "LOCATION": assessment.location,
        "SUMMARY": assessment.summary,
        "STATUS": status,
        "MODEL_ID": assessment.model_id,
        "RAW_TEXT": signal.raw_text,
        "ASSESSMENT_JSON": assessment.model_dump_json(),
        "CREATED_AT": db_ts(),
    })


def set_status(signal_id: str, status: str, db: Db, run_id: str | None = None) -> None:
    if run_id:
        db.execute("UPDATE FF_AG_DISRUPTION SET STATUS = ?, RUN_ID = ? WHERE SIGNAL_ID = ?", (status, run_id, signal_id))
    else:
        db.execute("UPDATE FF_AG_DISRUPTION SET STATUS = ? WHERE SIGNAL_ID = ?", (status, signal_id))


def load_recorded(signal_id: str, db: Db) -> tuple[DisruptionSignalIn, DisruptionAssessment] | None:
    df = try_query(db, "SELECT SIGNAL_ID, RECEIVED_AT, SOURCE, SOURCE_URL, RAW_TEXT, ASSESSMENT_JSON "
                       "FROM FF_AG_DISRUPTION WHERE SIGNAL_ID = ?", (signal_id,))
    if df is None or df.empty:
        return None
    r = df.iloc[0]
    signal = DisruptionSignalIn(signal_id=str(r["SIGNAL_ID"]), raw_text=str(r["RAW_TEXT"] or ""),
                                source=str(r["SOURCE"]), source_url=r["SOURCE_URL"] or None)
    return signal, DisruptionAssessment.model_validate_json(str(r["ASSESSMENT_JSON"]))


def already_seen(signal_ids: Sequence[str], db: Db) -> set[str]:
    """Signal ids already in FF_AG_DISRUPTION (feed scans skip them)."""
    ids = list(dict.fromkeys(signal_ids))
    if not ids:
        return set()
    df = try_query(db, f"SELECT SIGNAL_ID FROM FF_AG_DISRUPTION WHERE SIGNAL_ID IN ({', '.join('?' for _ in ids)})", ids)
    return set() if df is None else set(df["SIGNAL_ID"].astype(str))


def recent(db: Db, limit: int = 50) -> list[dict[str, Any]]:
    df = try_query(db, "SELECT SIGNAL_ID, RUN_ID, RECEIVED_AT, SOURCE, EVENT_TYPE, SEVERITY, CONFIDENCE, LOCATION, "
                       f"SUMMARY, STATUS, MODEL_ID FROM FF_AG_DISRUPTION ORDER BY CREATED_AT DESC LIMIT {int(limit)}")
    if df is None or df.empty:
        return []
    return df.where(df.notna(), None).to_dict(orient="records")
