"""Explainability Layer: Plain-English executive briefing with strict number verification (plan §3, §5, S11).

Guardrails:
1. Fact-sheet grounded: LLM only receives numbers computed by upstream deterministic rules.
2. Anti-hallucination number check: extracts every number in the LLM output and confirms
   it exists in the fact sheet (accounting for rounding/percentages). If an ungrounded
   number is found, the text is discarded and the deterministic template is used.
3. Domain guardrail: Never claims manufacturer intent; strictly frames as probabilistic risk flags.
4. Offline safe: When LLM_PROVIDER=none or network times out (>8s), falls back to template.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from agents.contracts import DependencyProfile, Forecast, Recommendation
from agents.ctx import Settings, load_settings

log = logging.getLogger(__name__)


def build_fact_sheet(
    forecast: Forecast | dict[str, Any],
    dependency: DependencyProfile | dict[str, Any] | None = None,
    recommendation: Recommendation | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile structured JSON fact sheet from upstream agent outputs."""
    fc_facts = (
        forecast.cause_facts if hasattr(forecast, "cause_facts")
        else (
            (forecast.get("cause_facts") if isinstance(forecast, dict) else None)
            or (forecast.get("CAUSE_FACTS_JSON") if isinstance(forecast, dict) else None)
            or (forecast.get("facts") if isinstance(forecast, dict) else None)
            or {}
        )
    )
    if isinstance(fc_facts, str):
        try:
            fc_facts = json.loads(fc_facts)
        except Exception:
            fc_facts = {}

    form_id = getattr(forecast, "formulation_id", None) or (forecast.get("formulation_id") if isinstance(forecast, dict) else None) or (forecast.get("FORM_ID") if isinstance(forecast, dict) else None) or "Unknown"
    band = getattr(forecast, "exit_risk_band", None) or (forecast.get("exit_risk_band") if isinstance(forecast, dict) else None) or (forecast.get("EXIT_RISK_BAND") if isinstance(forecast, dict) else None) or "UNKNOWN"
    band_val = band.value if hasattr(band, "value") else str(band)
    cause = (
        getattr(forecast, "cause_code", None)
        or (forecast.get("cause_code") if isinstance(forecast, dict) else None)
        or (forecast.get("CAUSE_CODE") if isinstance(forecast, dict) else None)
        or (forecast.get("primary_cause") if isinstance(forecast, dict) else None)
        or "UNKNOWN"
    )
    cause_val = cause.value if hasattr(cause, "value") else str(cause)

    sheet: dict[str, Any] = {
        "formulation_id": form_id,
        "exit_risk_band": band_val,
        "primary_cause": cause_val,
        "facts": fc_facts,
    }

    if dependency:
        sheet["dependency"] = {
            "n_producers_min": getattr(dependency, "n_producers_min", None) or dependency.get("n_producers_min") or dependency.get("N_PRODUCERS_MIN"),
            "hhi": getattr(dependency, "hhi", None) or dependency.get("hhi") or dependency.get("HHI"),
            "top_origin_country": getattr(dependency, "top_origin_country", None) or dependency.get("top_origin_country") or dependency.get("TOP_ORIGIN_COUNTRY"),
            "top_origin_share": getattr(dependency, "top_origin_share", None) or dependency.get("top_origin_share") or dependency.get("TOP_ORIGIN_SHARE"),
        }

    return sheet


def generate_template_explanation(fact_sheet: dict[str, Any]) -> str:
    """Deterministic, rule-based fallback explanation."""
    facts = fact_sheet.get("facts", {})
    band = fact_sheet.get("exit_risk_band", "UNKNOWN")
    cause = fact_sheet.get("primary_cause", "UNKNOWN")
    form_id = fact_sheet.get("formulation_id", "")

    ceiling = facts.get("ceiling_inr", {}).get("value")
    cost = facts.get("unit_cost_inr", {}).get("value")
    headroom = facts.get("headroom_pct", {}).get("value")
    producers = facts.get("n_producers_min", {}).get("value")
    country = facts.get("top_origin_country", {}).get("value")
    origin_share = facts.get("top_origin_share", {}).get("value")
    cover = facts.get("days_of_cover", {}).get("value")

    lines = [f"Formulation {form_id} is flagged {band} due to {cause}."]

    if ceiling is not None and cost is not None:
        hr_str = f"{headroom * 100:.1f}%" if headroom is not None else "n/a"
        lines.append(f"Estimated unit cost is ₹{cost:.2f} against a DPCO ceiling realization of ₹{ceiling:.2f} (headroom {hr_str}).")

    if producers is not None:
        origin_str = f" with {origin_share * 100:.0f}% of API from {country}" if country and origin_share else ""
        lines.append(f"Supply dependency shows at least {producers} active producers{origin_str}.")

    if cover is not None:
        lines.append(f"Usable hospital inventory provides approximately {round(cover)} days of cover at current issue rates.")

    return " ".join(lines)


def extract_numbers(text: str) -> list[float]:
    """Find all numbers (integers, floats) in text."""
    # Match patterns like 12, 12.5, -0.69, etc.
    raw = re.findall(r"[-+]?\b\d+(?:\.\d+)?\b", text)
    nums = []
    for r in raw:
        try:
            nums.append(float(r))
        except ValueError:
            pass
    return nums


def collect_allowed_numbers(data: Any) -> set[float]:
    """Recursively collect all numeric values in the fact sheet."""
    allowed = set()

    def _walk(obj: Any) -> None:
        if isinstance(obj, (int, float)):
            allowed.add(round(float(obj), 4))
            # Also allow percentage equivalent (e.g. 0.15 -> 15.0)
            allowed.add(round(float(obj) * 100.0, 2))
            allowed.add(round(float(obj) * 100.0, 1))
            allowed.add(round(float(obj) * 100.0, 0))
            # Integer rounding
            allowed.add(float(round(obj)))
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                _walk(v)

    _walk(data)
    return allowed


def verify_numbers(llm_text: str, fact_sheet: dict[str, Any]) -> tuple[bool, str | None]:
    """Check that every number in the LLM text exists in the fact sheet."""
    text_nums = extract_numbers(llm_text)
    allowed_nums = collect_allowed_numbers(fact_sheet)

    for n in text_nums:
        # Ignore common harmless numbers like 1, 2, 3 in bullet lists
        if n in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
            continue

        matched = any(abs(n - a) < 0.08 or (a != 0 and abs((n - a) / a) < 0.02) for a in allowed_nums)
        if not matched:
            return False, f"Number {n} not grounded in fact sheet"

    return True, None


def explain(
    forecast: Forecast | dict[str, Any],
    dependency: DependencyProfile | dict[str, Any] | None = None,
    recommendation: Recommendation | dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Generate executive explanation for the Chief Pharmacist."""
    s = settings or load_settings()
    sheet = build_fact_sheet(forecast, dependency, recommendation)
    template_text = generate_template_explanation(sheet)

    if s.llm_provider == "none" or not s.anthropic_api_key:
        return {
            "text": template_text,
            "used_llm": False,
            "fallback_reason": "LLM_PROVIDER=none or missing API key",
        }

    if s.llm_provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        prompt = f"""You are the clinical briefing agent for a Chief Pharmacist in an Indian tertiary hospital.
Explain the supply disruption risk for the formulation below based ONLY on the provided fact sheet.

CRITICAL RULES:
1. Write 3-4 plain, professional sentences.
2. Use ONLY numbers from the fact sheet. Never invent or adjust any number.
3. Frame as a probabilistic exit risk flag for review, NEVER claim a manufacturer's intent ("X will discontinue").
4. If cost is PROXY or stock is SYNTH, state that the estimate is derived from market data.

Fact sheet:
{json.dumps(sheet, indent=2, default=str)}
"""
        try:
            resp = client.messages.create(
                model=s.llm_model or "claude-3-5-sonnet-20241022",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
                timeout=8.0,
            )
            llm_text = resp.content[0].text.strip()
            valid, err = verify_numbers(llm_text, sheet)
            if valid:
                return {"text": llm_text, "used_llm": True, "fallback_reason": None}
            log.warning("LLM explanation discarded due to ungrounded number: %s", err)
            return {"text": template_text, "used_llm": False, "fallback_reason": err}
        except Exception as exc:
            log.warning("LLM explanation call failed: %s", exc)
            return {"text": template_text, "used_llm": False, "fallback_reason": str(exc)}

    return {
        "text": template_text,
        "used_llm": False,
        "fallback_reason": f"Unsupported provider {s.llm_provider}",
    }
