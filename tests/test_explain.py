"""Unit tests for Stage S11: Explainability layer and anti-hallucination number checks."""
from __future__ import annotations

import pytest

from agents import explain
from agents.ctx import Settings


def _sample_fact_sheet() -> dict:
    return {
        "formulation_id": "BENZYL-PENICILLIN",
        "exit_risk_band": "RED",
        "primary_cause": "CEILING_BELOW_COST",
        "facts": {
            "ceiling_inr": {"value": 14.92, "source": "FF_REF_CEILING_PRICE"},
            "unit_cost_inr": {"value": 15.50, "source": "TradeStat proxy"},
            "headroom_pct": {"value": -0.0389, "source": "A1 Margin Sentinel"},
            "n_producers_min": {"value": 3, "source": "FF_REF_PRODUCER"},
            "top_origin_country": {"value": "China", "source": "TradeStat"},
            "top_origin_share": {"value": 0.72, "source": "TradeStat"},
            "days_of_cover": {"value": 21.0, "source": "FF_V_DAYS_OF_COVER"},
        }
    }


def test_explain_none_provider_uses_template() -> None:
    settings = Settings(llm_provider="none")
    sheet = _sample_fact_sheet()
    res = explain.explain(sheet, settings=settings)
    assert res["used_llm"] is False
    assert "BENZYL-PENICILLIN" in res["text"]
    assert "CEILING_BELOW_COST" in res["text"]
    assert "14.92" in res["text"]
    assert "15.50" in res["text"]


def test_verify_numbers_accepts_grounded_numbers() -> None:
    sheet = _sample_fact_sheet()
    valid_text = (
        "The ceiling price is 14.92 INR while estimated cost has reached 15.50 INR. "
        "The hospital has 21 days of cover remaining with 3 producers active."
    )
    valid, err = explain.verify_numbers(valid_text, sheet)
    assert valid is True
    assert err is None


def test_verify_numbers_catches_hallucinated_number() -> None:
    sheet = _sample_fact_sheet()
    hallucinated_text = (
        "The ceiling price is 14.92 INR and the company will suffer a 999.00 INR loss. "
        "Hospital cover is 21 days."
    )
    valid, err = explain.verify_numbers(hallucinated_text, sheet)
    assert valid is False
    assert "Number 999.0 not grounded in fact sheet" in err
