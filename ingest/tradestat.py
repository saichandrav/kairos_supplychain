"""API cost and import origin: FF_REF_API, FF_REF_FORM_API, FF_REF_BOM_ASSUMPTION, FF_REF_API_COST_MONTHLY,
FF_REF_API_ORIGIN (plan §4.1, §5).

    python -m ingest.tradestat          # write the five data/seed/FF_REF_*.csv files

TradeStat (MEIDB) only answers one HS code x one month per query, so it could not be downloaded in time
(docs/STATUS.md). Cost and origin are therefore **SYNTH** (IS_PROXY='SYNTH'), generated from
rules/bom_defaults.yaml and the REAL ceiling prices:
  * each API's current cost per kg is set so its tightest target formulation starts with a headroom drawn
    from HEADROOM_RANGE (§4.1 formula, same margins as A1);
  * the monthly history is a seeded random walk whose drift and volatility are drawn per API WITHOUT
    looking at Para-19 labels. The S12 backtest on these series only exercises the pipeline; it is not
    evidence that the signal works.
The table stores the monthly unit value; A1 applies the 3-month rolling median itself
(rules/weights.yaml margin.api_cost_median_months), so it is not smoothed twice.
`unit_value_inr_per_kg` / `rolling_median` are the calculation for real TradeStat exports.
"""
from __future__ import annotations

import csv
import hashlib
import sys
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from agents.ctx import REPO_ROOT
from agents.rules import load_rules

SEED_DIR = REPO_ROOT / "data" / "seed"
BOM_YAML = REPO_ROOT / "rules" / "bom_defaults.yaml"
API_HS8 = SEED_DIR / "api_hs8.csv"
GLOBAL_SEED = 20260927
FIRST_MONTH, LAST_MONTH = date(2019, 1, 1), date(2026, 9, 1)   # cost history (S12 backtest goes back to 2019)
ORIGIN_MONTHS = 36
HEADROOM_RANGE = (0.12, 0.40)
ORIGIN_COUNTRIES = ("CN", "IT", "DE", "US", "JP", "KR", "ES", "CH")
SYNTH_SOURCE = "Kairos synthetic (TradeStat not downloaded; see ingest/tradestat.py)"
FETCHED_AT = "2026-09-27T00:00:00Z"

_QTY_TO_KG = {"KGS": 1.0, "KG": 1.0, "TON": 1000.0, "MTS": 1000.0, "MT": 1000.0, "GMS": 0.001, "G": 0.001}
_VALUE_TO_INR = {"CRORE": 1e7, "LAKH": 1e5, "INR": 1.0}


# ---------------------------------------------------------------- unit value (real TradeStat path)


def unit_value_inr_per_kg(value: float, value_unit: str, qty: float, qty_unit: str) -> float | None:
    """Import unit value in INR/kg from a TradeStat row (value in ₹ crore/lakh/INR, quantity in KGS/TON/...).
    None when the row cannot give a unit value (no quantity, zero value, unknown unit)."""
    v_mult = _VALUE_TO_INR.get(value_unit.strip().upper())
    q_mult = _QTY_TO_KG.get(qty_unit.strip().upper())
    if v_mult is None or q_mult is None or qty is None or value is None:
        return None
    kg = float(qty) * q_mult
    inr = float(value) * v_mult
    if kg <= 0 or inr <= 0:
        return None
    return inr / kg


def rolling_median(values: list[float | None], months: int = 3) -> list[float | None]:
    """Trailing median over `months` points, skipping missing months (min 1 observation)."""
    s = pd.Series(values, dtype="float64")
    return [None if pd.isna(x) else float(x) for x in s.rolling(months, min_periods=1).median()]


# ---------------------------------------------------------------- SYNTH generation


def _rng(*parts: str) -> np.random.Generator:
    digest = hashlib.sha256("|".join((str(GLOBAL_SEED),) + parts).encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "little"))


def _months(first: date, last: date) -> list[date]:
    return [d.date() for d in pd.date_range(first, last, freq="MS")]


def _prov(is_proxy: str, source: str, url: str = "") -> dict[str, str]:
    return {"SOURCE": source, "SOURCE_URL": url, "FETCHED_AT": FETCHED_AT, "IS_PROXY": is_proxy}


def load_inputs() -> tuple[dict[str, Any], list[dict[str, str]], dict[str, float]]:
    """(bom yaml, api_hs8 rows, current ceiling price per FORM_ID from data/seed/FF_REF_CEILING_PRICE.csv)."""
    bom = yaml.safe_load(BOM_YAML.read_text(encoding="utf-8"))
    with API_HS8.open(encoding="utf-8") as fh:
        mapping = list(csv.DictReader(fh))
    with (SEED_DIR / "FF_REF_CEILING_PRICE.csv").open(encoding="utf-8") as fh:
        rows = sorted(csv.DictReader(fh), key=lambda r: r["EFFECTIVE_FROM"])
    ceiling = {r["FORM_ID"]: float(r["CEILING_PRICE"]) for r in rows}  # latest wins
    return bom, mapping, ceiling


def api_budget_per_kg(ceiling: float, other_cost: float, mg: float, yld: float, headroom: float,
                      retailer: float, wholesaler: float) -> float | None:
    """API cost per kg that gives `headroom` for one formulation under the §4.1 formula (None if impossible)."""
    realisation = ceiling / (1 + retailer) / (1 + wholesaler)
    api_part = realisation * (1 - headroom) - other_cost
    if api_part <= 0 or mg <= 0:
        return None
    return api_part * yld * 1e6 / mg


def build(bom: dict[str, Any], mapping: list[dict[str, str]], ceiling: dict[str, float]) -> dict[str, list[dict]]:
    """All five tables' rows. Deterministic for the same inputs."""
    rules = load_rules()
    retailer = float(rules.value("weights", "margin.retailer_margin"))
    wholesaler = float(rules.value("weights", "margin.wholesaler_margin"))
    yld = float(bom["yield"]["value"])
    kinds = bom["unit_kinds"]

    apis: dict[str, dict] = {}
    for m in mapping:
        if m["API_ID"] and m["API_ID"] not in apis:
            apis[m["API_ID"]] = {"API_ID": m["API_ID"], "NAME": m["API_NAME"], "HS8": m["HS8"], "KSM_ID": "",
                                 **_prov("PROXY", f"data/seed/api_hs8.csv (HS mapping, confidence {m['CONFIDENCE']})")}

    form_api, bom_rows, budgets = [], [], {}
    for form_id, spec in bom["formulations"].items():
        if form_id not in ceiling:
            raise ValueError(f"{form_id} in bom_defaults.yaml has no ceiling price in data/seed")
        k = kinds[spec["kind"]]
        other = k["conversion_inr"] + k["packaging_inr"] + k["freight_inr"]
        first = spec["apis"][0]
        bom_rows.append({"FORM_ID": form_id, "API_G_PER_UNIT": round(first["mg"] / 1000, 6), "YIELD": yld,
                         "CONVERSION_COST_INR": k["conversion_inr"], "PACKAGING_COST_INR": k["packaging_inr"],
                         "FREIGHT_COST_INR": k["freight_inr"],
                         **_prov("PROXY", f"rules/bom_defaults.yaml ({spec['kind']}: {k['source'][:60]})")})
        for a in spec["apis"]:
            if a["api"] not in apis:
                raise ValueError(f"{a['api']} is not in data/seed/api_hs8.csv")
            form_api.append({"FORM_ID": form_id, "API_ID": a["api"], "API_MG_PER_UNIT": a["mg"],
                             **_prov("PROXY", f"rules/bom_defaults.yaml: {a['source'][:90]}")})
        # combinations: split the API budget by mg share so the formulation keeps the drawn headroom
        total_mg = sum(a["mg"] for a in spec["apis"])
        for a in spec["apis"]:
            h0 = float(_rng("headroom", a["api"]).uniform(*HEADROOM_RANGE))
            b = api_budget_per_kg(ceiling[form_id], other, total_mg, yld, h0, retailer, wholesaler)
            if b is not None:
                budgets.setdefault(a["api"], []).append(b)

    cost_rows, origin_rows = [], []
    months = _months(FIRST_MONTH, LAST_MONTH)
    origin_months = months[-ORIGIN_MONTHS:]
    for api_id in sorted(apis):
        if api_id not in budgets:
            continue  # every formulation of this API is below water on non-API cost alone: no cost series
        rng = _rng("cost", api_id)
        current = min(budgets[api_id])                   # tightest formulation starts at its drawn headroom
        drift = rng.normal(0.004, 0.006)                 # per month, independent of any Para-19 label
        vol = rng.uniform(0.02, 0.05)
        steps = rng.normal(drift, vol, len(months))
        path = np.exp(np.cumsum(steps) - np.cumsum(steps)[-1])   # ends at 1.0 = current cost
        qty = rng.lognormal(mean=np.log(rng.uniform(2e3, 5e5)), sigma=0.35, size=len(months))
        for m, f, q in zip(months, path, qty):
            cost_rows.append({"API_ID": api_id, "MONTH": m.isoformat(), "UNIT_VALUE_INR_KG": round(current * f, 4),
                              "QTY_KG": round(float(q), 3), **_prov("SYNTH", SYNTH_SOURCE)})
        o = _rng("origin", api_id)
        top = "CN" if o.random() < 0.7 else str(o.choice(ORIGIN_COUNTRIES[1:]))
        others = [c for c in o.permutation(ORIGIN_COUNTRIES) if c != top][: int(o.integers(1, 4))]
        top_share = o.uniform(0.45, 0.92)
        weights = o.dirichlet(np.ones(len(others))) * (1 - top_share)
        base = dict(zip([top, *others], [top_share, *weights]))
        for m in origin_months:
            jitter = {c: max(0.0, s + o.normal(0, 0.03)) for c, s in base.items()}
            total = sum(jitter.values())
            for c, s in jitter.items():
                origin_rows.append({"API_ID": api_id, "MONTH": m.isoformat(), "COUNTRY": c,
                                    "SHARE": round(s / total, 6), **_prov("SYNTH", SYNTH_SOURCE)})

    return {"FF_REF_API": list(apis.values()), "FF_REF_FORM_API": form_api, "FF_REF_BOM_ASSUMPTION": bom_rows,
            "FF_REF_API_COST_MONTHLY": cost_rows, "FF_REF_API_ORIGIN": origin_rows}


def write_seeds(tables: dict[str, list[dict]], seed_dir: Path = SEED_DIR) -> dict[str, int]:
    counts = {}
    for name, rows in tables.items():
        with (seed_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        counts[name] = len(rows)
    return counts


def main() -> int:
    counts = write_seeds(build(*load_inputs()))
    print(" | ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
