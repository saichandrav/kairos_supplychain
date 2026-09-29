"""Load and validate rules/*.yaml (plan §3 A5, §4).

Every constant is `{value, source, verify}`. Code reads numbers through
`Rules.value("weights", "margin.retailer_margin")`; never hard-code them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"
FILES = ("weights", "dpco", "sop_controls", "disruption")

# Keys that must exist so agents can rely on them (plan §4).
REQUIRED_CONSTANTS: dict[str, tuple[str, ...]] = {
    "weights": (
        "margin.retailer_margin", "margin.wholesaler_margin", "margin.api_cost_median_months",
        "margin.trend_window_min_months", "margin.trend_window_max_months",
        "bands.red_headroom_pct", "bands.red_months_to_breach",
        "bands.amber_headroom_pct", "bands.amber_months_to_breach",
        "concentration.w_inv_producers", "concentration.w_hhi", "concentration.w_top_origin_share",
        "exit_risk.a_neg_headroom", "exit_risk.b_inv_months_to_breach", "exit_risk.c_concentration",
        "exit_risk.d_nsq_recent", "exit_risk.e_otd_drop",
        "exposure.cover_ratio_min", "exposure.cover_ratio_max", "exposure.days_of_cover_floor",
        "criticality.V", "criticality.E", "criticality.D", "criticality.nlem_core_multiplier",
        "watchlist.top_n",
    ),
    "dpco": ("constants.gst_rate_medicines", "constants.discontinuation_notice_months"),
    "disruption": (
        "sensing.escalate_confidence_below", "scenario.lead_time_passthrough", "logistics.carriers",
        "inventory.service_z.V", "governance.human_cost_threshold_inr", "governance.auto_run_budget_inr",
    ),
}
# The ten A5 checks from the plan §3 A5 table.
REQUIRED_CHECKS: dict[str, tuple[str, ...]] = {
    "dpco": ("PRICE_COMPLIANCE", "SCHEDULED_STATUS"),
    "sop_controls": (
        "SUPPLIER_LICENCE", "QUALITY_HISTORY", "SHELF_LIFE", "EXPIRY_WASTE",
        "BUDGET", "STORAGE", "ROL_ROQ", "CLINICAL",
    ),
}
CHECK_KEYS = ("id", "name", "rule", "source", "verify", "on_fail")


class RulesError(ValueError):
    pass


def _is_constant(node: Any) -> bool:
    return isinstance(node, dict) and "value" in node


def _validate_constant(node: Any, where: str) -> None:
    if not isinstance(node, dict) or set(node) != {"value", "source", "verify"}:
        raise RulesError(f"{where}: constant must have exactly value, source, verify")
    if not isinstance(node["source"], str) or not node["source"].strip():
        raise RulesError(f"{where}: source must be a non-empty string")
    if not isinstance(node["verify"], bool):
        raise RulesError(f"{where}: verify must be true or false")
    if node["value"] is None:
        raise RulesError(f"{where}: value is empty")


def _walk_constants(node: Any, path: str) -> None:
    if _is_constant(node):
        _validate_constant(node, path)
    elif isinstance(node, dict):
        for k, v in node.items():
            _walk_constants(v, f"{path}.{k}" if path else str(k))
    else:
        raise RulesError(f"{path}: expected a {{value, source, verify}} constant or a group")


def _validate_checks(checks: Any, name: str) -> None:
    if not isinstance(checks, list):
        raise RulesError(f"{name}.checks must be a list")
    seen: set[str] = set()
    for c in checks:
        missing = [k for k in CHECK_KEYS if k not in c]
        if missing:
            raise RulesError(f"{name}: check {c.get('id', '?')} missing {missing}")
        if c["id"] in seen:
            raise RulesError(f"{name}: duplicate check id {c['id']}")
        seen.add(c["id"])
        if c["on_fail"] not in ("FAIL", "WARN"):
            raise RulesError(f"{name}: check {c['id']} on_fail must be FAIL or WARN")
        if not isinstance(c["verify"], bool):
            raise RulesError(f"{name}: check {c['id']} verify must be true or false")
        for p, v in (c.get("params") or {}).items():
            _validate_constant(v, f"{name}.{c['id']}.params.{p}")


def validate(name: str, doc: dict[str, Any]) -> None:
    """Raise RulesError if a rules document is malformed or missing a required key."""
    if not isinstance(doc, dict) or not isinstance(doc.get("version"), str):
        raise RulesError(f"{name}: needs a string 'version'")
    for key, node in doc.items():
        if key == "version":
            continue
        if key == "checks":
            _validate_checks(node, name)
        else:
            _walk_constants(node, f"{key}")
    for path in REQUIRED_CONSTANTS.get(name, ()):
        if not _is_constant(_lookup(doc, path)):
            raise RulesError(f"{name}: missing constant {path}")
    ids = {c["id"] for c in doc.get("checks", [])}
    for cid in REQUIRED_CHECKS.get(name, ()):
        if cid not in ids:
            raise RulesError(f"{name}: missing check {cid}")


def _lookup(doc: dict[str, Any], path: str) -> Any:
    node: Any = doc
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@dataclass(frozen=True)
class Rules:
    docs: dict[str, dict[str, Any]]

    def constant(self, file: str, path: str) -> dict[str, Any]:
        node = _lookup(self.docs[file], path)
        if not _is_constant(node):
            raise KeyError(f"{file}:{path}")
        return node

    def value(self, file: str, path: str) -> Any:
        return self.constant(file, path)["value"]

    def checks(self, file: str | None = None) -> list[dict[str, Any]]:
        files = [file] if file else [f for f in FILES if "checks" in self.docs[f]]
        return [c for f in files for c in self.docs[f].get("checks", [])]

    def check(self, check_id: str) -> dict[str, Any]:
        for c in self.checks():
            if c["id"] == check_id:
                return c
        raise KeyError(check_id)

    def param(self, check_id: str, name: str) -> Any:
        return self.check(check_id)["params"][name]["value"]

    @property
    def versions(self) -> dict[str, str]:
        return {f: d["version"] for f, d in self.docs.items()}


def load_rules(rules_dir: str | Path = RULES_DIR) -> Rules:
    """Load and validate all rules files."""
    rules_dir = Path(rules_dir)
    docs: dict[str, dict[str, Any]] = {}
    for name in FILES:
        path = rules_dir / f"{name}.yaml"
        with path.open(encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        validate(name, doc)
        docs[name] = doc
    return Rules(docs)
