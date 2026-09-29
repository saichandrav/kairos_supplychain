"""Agent inputs and outputs: the single source of truth (plan §3, §4).

Every agent is `run(input, ctx) -> output` over these models. Numbers are
computed by deterministic rules (`rules/*.yaml`); the LLM never fills them.
Money is INR with 4 decimals (HANA DECIMAL(15,4)).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Money = Annotated[Decimal, Field(max_digits=15, decimal_places=4)]
Share = Annotated[float, Field(ge=0.0, le=1.0)]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------- enums


class SignalBand(str, Enum):
    """A1 signal bands (plan §4.1)."""

    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"


class CauseCode(str, Enum):
    """A3 stated cause of the exit-risk flag (plan §3 A3)."""

    CEILING_BELOW_COST = "CEILING_BELOW_COST"
    HEADROOM_ERODING = "HEADROOM_ERODING"
    SINGLE_ORIGIN_API = "SINGLE_ORIGIN_API"
    FEW_PRODUCERS = "FEW_PRODUCERS"
    QUALITY_NSQ = "QUALITY_NSQ"
    SUPPLIER_OTD_DROP = "SUPPLIER_OTD_DROP"
    LOGISTICS_DELAY = "LOGISTICS_DELAY"


class DataTag(str, Enum):
    """Provenance label shown next to every fact (plan §5; IS_PROXY column)."""

    REAL = "REAL"
    PROXY = "PROXY"
    SYNTH = "SYNTH"


class CheckStatus(str, Enum):
    """A5 per-check result (plan §3 A5)."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class Overall(str, Enum):
    """A5 overall verdict; only READY / NEEDS_CHANGES reach the human gate (plan §3 A5)."""

    READY = "READY"
    NEEDS_CHANGES = "NEEDS_CHANGES"
    BLOCKED = "BLOCKED"


class OptionType(str, Enum):
    """A4 scenario option types (plan §3 A4 a–d)."""

    BUFFER = "BUFFER"
    ALT_SUPPLIER = "ALT_SUPPLIER"
    THERAPEUTIC_ALT = "THERAPEUTIC_ALT"
    REBALANCE = "REBALANCE"


class ApprovalAction(str, Enum):
    """Human-gate actions (plan §3 human gate)."""

    APPROVE = "APPROVE"
    EDIT_APPROVE = "EDIT_APPROVE"
    REJECT = "REJECT"
    SNOOZE = "SNOOZE"


class RunTrigger(str, Enum):
    """Why a run started (plan §3 A1 trigger)."""

    NIGHTLY = "NIGHTLY"
    ON_DEMAND = "ON_DEMAND"
    SHOCK = "SHOCK"
    DISRUPTION = "DISRUPTION"


# ---------------------------------------------------------------- shared parts


class DataSourceRef(_Model):
    """Provenance of an input: SOURCE / SOURCE_URL / FETCHED_AT / IS_PROXY (plan §5)."""

    source: str
    source_url: str | None = None
    fetched_at: datetime | None = None
    is_proxy: DataTag


class DataQuality(_Model):
    """Confidence = share of REAL inputs × freshness (plan §4.3)."""

    confidence: Share
    tags: dict[str, DataTag] = Field(default_factory=dict)


class ShockSpec(_Model):
    """On-demand shock, e.g. API import cost +30% (plan §3 A1 trigger)."""

    api_cost_pct: float = 0.0
    api_ids: list[str] = Field(default_factory=list, description="empty = all APIs")
    label: str | None = None


class RunContext(_Model):
    """Per-run input shared by the orchestrator with every agent (plan §2, §3)."""

    run_id: str
    started_at: datetime = Field(default_factory=_utcnow)
    trigger: RunTrigger = RunTrigger.ON_DEMAND
    formulation_ids: list[str] = Field(default_factory=list, description="empty = all tracked")
    as_of: date | None = None
    shock: ShockSpec | None = None
    dry_run: bool = False
    rule_versions: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------- A1


class SecondarySignal(_Model):
    """A1 secondary inputs: NSQ alerts, supplier OTD, cold-chain excursions (plan §3 A1)."""

    kind: Literal["NSQ", "OTD", "COLD_CHAIN"]
    value: float
    tag: DataTag
    note: str | None = None


class RiskSignal(_Model):
    """A1 Margin Sentinel output → FF_AG_SIGNAL (plan §3 A1, §4.1)."""

    run_id: str
    formulation_id: str
    band: SignalBand
    headroom_pct: float
    headroom_trend: float = Field(description="OLS slope of headroom_pct per month")
    months_to_breach: float | None = Field(default=None, ge=0, description="only when trend < 0")
    realisation_inr: Money | None = None
    unit_cost_inr: Money | None = None
    secondary_signals: list[SecondarySignal] = Field(default_factory=list)
    data_quality: DataQuality


# ---------------------------------------------------------------- A2


class DependencyProfile(_Model):
    """A2 Dependency Mapper output → FF_AG_DEPENDENCY (plan §3 A2, §4.2).

    `n_producers_min` is a lower bound: render as "at least N producers".
    """

    run_id: str
    formulation_id: str
    n_producers_min: int = Field(ge=0)
    hhi: Share
    top_origin_country: str | None = None
    top_origin_share: Share = 0.0
    concentration: Share | None = None
    affected_materials: list[str] = Field(default_factory=list)
    affected_wards: list[str] = Field(default_factory=list)
    data_quality: DataQuality | None = None


# ---------------------------------------------------------------- A3


class Forecast(_Model):
    """A3 Shortage Forecaster output (plan §3 A3, §4.3). A probabilistic flag for review."""

    run_id: str
    formulation_id: str
    exit_risk_band: SignalBand
    exit_risk: Share | None = None
    exposure: float | None = Field(default=None, ge=0)
    window_months_lo: float | None = Field(default=None, ge=0)
    window_months_hi: float | None = Field(default=None, ge=0)
    days_of_cover: float | None = Field(default=None, ge=0)
    stockout_date_if_exit: date | None = None
    cause_code: CauseCode
    cause_facts: dict[str, Any] = Field(default_factory=dict)
    confidence: Share | None = None

    @model_validator(mode="after")
    def _window_order(self) -> "Forecast":
        lo, hi = self.window_months_lo, self.window_months_hi
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("window_months_lo must be <= window_months_hi")
        return self


# ---------------------------------------------------------------- A4


class Scenario(_Model):
    """A4 Resilience Simulator option → FF_AG_SCENARIO (plan §3 A4)."""

    scenario_id: str
    run_id: str
    formulation_id: str
    option_type: OptionType
    supplier: str | None = None
    material: str | None = None
    qty: float = Field(ge=0)
    unit_rate_inr: Money | None = Field(default=None, description="GST-inclusive rate the hospital pays per unit")
    cost: Money
    coverage_days: float = Field(ge=0)
    expiry_waste_risk: Share = 0.0
    correlated_risk_flag: bool = False
    correlated_risk_reason: str | None = None
    rank: int | None = Field(default=None, ge=1, description="None = rejected (not offered to the gate)")
    plant: str | None = None
    cold_chain: bool = False
    residual_shelf_life_months: float | None = Field(default=None, ge=0)
    substitute_formulation_id: str | None = Field(default=None, description="THERAPEUTIC_ALT: the substitute")


# ---------------------------------------------------------------- A5


class CheckResult(_Model):
    """One A5 check with evidence (plan §3 A5 table; rules/dpco.yaml, rules/sop_controls.yaml)."""

    check_id: str
    name: str
    status: CheckStatus
    evidence: dict[str, Any] = Field(default_factory=dict)
    rule_source: str | None = None


class DraftPR(_Model):
    """Purchase requisition item shaped on S/4HANA API_PURCHASEREQ_PROCESS_SRV
    (A_PurchaseReqnItem) (plan §3 A6). Aliases are the S/4 field names."""

    material: str = Field(alias="Material")
    plant: str = Field(alias="Plant")
    quantity: float = Field(gt=0, alias="RequestedQuantity")
    base_unit: str = Field(default="EA", alias="BaseUnit")
    delivery_date: date = Field(alias="DeliveryDate")
    purchasing_group: str = Field(alias="PurchasingGroup")
    fixed_supplier: str | None = Field(default=None, alias="FixedSupplier")
    unit_price_inr: Money | None = Field(default=None, alias="PurchaseRequisitionPrice")
    currency: str = Field(default="INR", alias="PurReqnItemCurrency")

    def to_s4_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json", exclude_none=True)


class Recommendation(_Model):
    """A5 Validator output (plan §3 A5). Only READY / NEEDS_CHANGES reach the gate."""

    run_id: str
    scenario_id: str
    checks: list[CheckResult]
    overall: Overall
    needs_second_approver: bool = False
    draft_pr: DraftPR | None = None

    @model_validator(mode="after")
    def _ready_has_no_fail(self) -> "Recommendation":
        if self.overall is Overall.READY and any(c.status is CheckStatus.FAIL for c in self.checks):
            raise ValueError("overall READY is not allowed when a check FAILs")
        return self

    @property
    def reaches_gate(self) -> bool:
        return self.overall in (Overall.READY, Overall.NEEDS_CHANGES)


# ---------------------------------------------------------------- human gate


class ApprovalChecklist(_Model):
    """Manual verification checklist; every item must be ticked to approve (plan §3 human gate)."""

    cp01_material_master: bool = False
    cp02_rol_roq_stock: bool = False
    cp04_supplier_verified: bool = False
    price_within_ceiling_gst: bool = False
    shelf_life_fefo_ok: bool = False
    cause_reviewed_not_claim: bool = False

    @property
    def complete(self) -> bool:
        return all(self.model_dump().values())


class ApprovalDecision(_Model):
    """Human decision → FF_AG_APPROVAL; A6 acts only on an approving row (plan §3 human gate, A6)."""

    run_id: str
    scenario_id: str
    approver: str
    level: Literal[1, 2] = 1
    action: ApprovalAction
    decided_at: datetime = Field(default_factory=_utcnow)
    checklist: ApprovalChecklist = Field(default_factory=ApprovalChecklist)
    reason: str | None = None
    edited_qty: float | None = Field(default=None, gt=0)
    edited_supplier: str | None = None
    snooze_days: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _action_rules(self) -> "ApprovalDecision":
        a = self.action
        if a in (ApprovalAction.APPROVE, ApprovalAction.EDIT_APPROVE) and not self.checklist.complete:
            raise ValueError("approval requires every checklist item to be ticked")
        if a is ApprovalAction.EDIT_APPROVE and self.edited_qty is None and not self.edited_supplier:
            raise ValueError("EDIT_APPROVE requires edited_qty or edited_supplier")
        if a is ApprovalAction.REJECT and not (self.reason and self.reason.strip()):
            raise ValueError("REJECT requires a reason")
        if a is ApprovalAction.SNOOZE and self.snooze_days is None:
            raise ValueError("SNOOZE requires snooze_days")
        return self

    @property
    def approves(self) -> bool:
        return self.action in (ApprovalAction.APPROVE, ApprovalAction.EDIT_APPROVE)


# ---------------------------------------------------------------- A6


class DisruptionCategory(str, Enum):
    """Disruption signal categories for the Disruption Sensing Agent."""

    MARITIME_SECURITY = "MARITIME_SECURITY"
    WEATHER = "WEATHER"
    TARIFF = "TARIFF"
    PORT_CONGESTION = "PORT_CONGESTION"
    GEOPOLITICAL = "GEOPOLITICAL"
    SUPPLIER = "SUPPLIER"
    OTHER = "OTHER"


class DisruptionSeverity(str, Enum):
    """Severity of a disruption signal, aligned with SignalBand."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class DisruptionSignalIn(_Model):
    """Raw external signal submitted for AI assessment."""

    signal_id: str = Field(description="Unique identifier for this signal")
    raw_text: str = Field(max_length=10000, description="The external signal text to classify")
    source: str = Field(description="Origin system or feed name")
    source_url: str | None = None
    received_at: datetime = Field(default_factory=_utcnow)
    context: dict[str, Any] = Field(default_factory=dict, description="Additional context for assessment")


class AffectedEntity(_Model):
    """An entity affected by a disruption (port, supplier, route, etc.)."""

    entity_type: str
    entity_name: str
    confidence: Share = 0.5


class AffectedSeaRoute(_Model):
    """A sea route affected by a disruption, resolved from rules/sea_routes.yaml."""

    origin: str = Field(description="Country code (e.g. CN, DE)")
    origin_name: str
    origin_ports: list[str] = Field(default_factory=list)
    destination_ports: list[str] = Field(default_factory=list)
    chokepoints: list[str] = Field(default_factory=list)
    disrupted_chokepoint: str | None = None
    transit_days_normal: int | None = None
    transit_days_rerouted: int | None = None
    reroute_via: str | None = None
    delay_days: int | None = Field(default=None, description="Extra transit days if rerouted")
    affected_formulations: list[str] = Field(default_factory=list, description="FORM_IDs dependent on this origin")


class DisruptionAssessment(_Model):
    """Structured AI assessment of a disruption signal."""

    signal_id: str
    event_type: DisruptionCategory
    summary: str = Field(max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    severity: DisruptionSeverity
    location: str | None = None
    event_time: str | None = Field(default=None, description="ISO timestamp or descriptive time if exact time unknown")
    source: str
    affected_entities: list[AffectedEntity] = Field(default_factory=list)
    potential_impact: str | None = Field(default=None, max_length=1000)
    confidence: Share
    evidence: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    requires_human_review: bool = True
    rationale: str | None = Field(default=None, max_length=500)
    assessed_at: datetime = Field(default_factory=_utcnow)
    model_id: str | None = None
    affected_sea_routes: list[AffectedSeaRoute] = Field(default_factory=list)
    total_affected_formulations: int = 0
    escalated: bool = False
    escalation_reason: str | None = None


# ---------------------------------------------------------------- Scenario Planning


class WhatIfKind(str, Enum):
    """What-if disruption types the Scenario Planning Agent can simulate."""

    CHOKEPOINT_CLOSURE = "CHOKEPOINT_CLOSURE"
    CHOKEPOINT_RISK = "CHOKEPOINT_RISK"
    PORT_CLOSURE = "PORT_CLOSURE"
    PORT_CONGESTION = "PORT_CONGESTION"
    TARIFF_INCREASE = "TARIFF_INCREASE"
    SUPPLIER_OFFLINE = "SUPPLIER_OFFLINE"
    ORIGIN_EXPORT_STOP = "ORIGIN_EXPORT_STOP"


class WhatIfSpec(_Model):
    """One simulated disruption. `target` is a chokepoint, Indian port, origin country code or vendor LIFNR."""

    kind: WhatIfKind
    target: str = Field(min_length=1, max_length=120)
    magnitude_pct: float = Field(default=0.0, ge=0, le=500, description="TARIFF_INCREASE: uplift in percent")
    duration_days: int = Field(default=30, ge=1, le=365)
    label: str | None = Field(default=None, max_length=200)


class StrategyType(str, Enum):
    """Response strategies compared on cost, time and risk."""

    ABSORB = "ABSORB"
    REROUTE = "REROUTE"
    AIR_EXPEDITE = "AIR_EXPEDITE"
    BUFFER_STOCK = "BUFFER_STOCK"
    ALT_SOURCING = "ALT_SOURCING"


class LaneExposure(_Model):
    """How one API origin lane of a formulation is hit by the disruption."""

    origin: str
    share: Share
    absorb_delay_days: float = Field(ge=0)
    reroute_delay_days: float | None = Field(default=None, ge=0)
    reroute_label: str | None = None
    security_risk: bool = False
    supply_loss: Share = 0.0
    tariff_pct: float = 0.0


class StrategyEval(_Model):
    """One strategy evaluated for one formulation. Numbers come from rules/disruption.yaml, never the LLM."""

    strategy: StrategyType
    feasible: bool
    cost_inr: Money
    time_to_effect_days: float = Field(ge=0)
    stockout_units: int = Field(ge=0)
    residual_risk: Share
    score: float | None = None
    note: str | None = None


class FormulationImpact(_Model):
    """Exposure of one stocked formulation to the simulated disruption."""

    formulation_id: str
    material: str
    name: str | None = None
    ved: str | None = None
    daily_consumption: float = Field(ge=0)
    usable_stock: float = Field(ge=0)
    days_of_cover: float | None = Field(default=None, ge=0)
    open_po_qty: float = Field(default=0.0, ge=0)
    unit_rate_inr: Money | None = None
    lanes: list[LaneExposure] = Field(default_factory=list)
    exposure_days_absorb: float = Field(ge=0)
    exposure_days_reroute: float = Field(ge=0)
    baseline_stockout_units: int = Field(default=0, ge=0, description="short even with no disruption")
    supply_loss_share: Share = 0.0
    tariff_uplift_pct: float = 0.0
    margin_band_after: SignalBand | None = None
    strategies: list[StrategyEval] = Field(default_factory=list)
    recommended: StrategyType | None = None


class PlanOption(_Model):
    """A plan-level option: one pure strategy for every exposed formulation, or the recommended mix."""

    option_id: str
    label: str
    cost_inr: Money
    time_to_effect_days: float = Field(ge=0)
    stockout_units: int = Field(ge=0)
    residual_risk: Share
    formulations_covered: int = Field(ge=0)
    score: float


class ScenarioPlan(_Model):
    """Scenario Planning Agent output → FF_AG_WHATIF."""

    plan_id: str
    run_id: str | None = None
    signal_id: str | None = None
    specs: list[WhatIfSpec]
    as_of: date
    impacts: list[FormulationImpact] = Field(default_factory=list)
    options: list[PlanOption] = Field(default_factory=list)
    recommended_option: str | None = None
    narrative: str | None = None
    narrative_used_llm: bool = False
    data_sources: list[DataSourceRef] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------- Rerouting & Logistics


class RouteChoice(_Model):
    """One feasible way to move an inbound consignment."""

    option_id: str
    label: str
    mode: Literal["SEA", "AIR"]
    carrier: str | None = None
    via: list[str] = Field(default_factory=list)
    new_eta: date
    delay_days: int
    extra_cost_inr: Money
    late_units: int = Field(default=0, ge=0)
    security_risk: bool = False


class ShipmentAction(_Model):
    """Logistics Agent re-plan of one open PO line (EKPO without goods receipt) → FF_AG_SHIPMENT_PLAN."""

    ebeln: str
    ebelp: int
    material: str
    formulation_id: str | None = None
    supplier: str | None = None
    origin: str | None = None
    qty: float = Field(ge=0)
    value_inr: Money
    old_eta: date
    stockout_date: date | None = None
    chosen: RouteChoice
    alternatives: list[RouteChoice] = Field(default_factory=list)
    decision_id: str | None = None
    status: str = "PLANNED"


class Notification(_Model):
    """Draft message to a downstream partner → FF_AG_NOTIFICATION. HELD until its decision is approved."""

    notification_id: str
    audience: Literal["SUPPLIER", "WARD", "PROCUREMENT", "FORWARDER"]
    recipient: str
    subject: str = Field(max_length=200)
    body: str = Field(max_length=4000)
    status: Literal["QUEUED", "HELD", "CANCELLED"] = "QUEUED"
    decision_id: str | None = None


class LogisticsPlan(_Model):
    run_id: str
    shipments: list[ShipmentAction] = Field(default_factory=list)
    notifications: list[Notification] = Field(default_factory=list)
    total_extra_cost_inr: Money = Decimal("0")
    carrier_load: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------- Inventory Rebalancing


class SafetyStockUpdate(_Model):
    """Recomputed safety stock / reorder point for the disrupted lead time → FF_AG_SAFETY_STOCK."""

    material: str
    plant: str
    formulation_id: str | None = None
    daily_mean: float = Field(ge=0)
    daily_sd: float = Field(ge=0)
    lead_time_days: float = Field(ge=0)
    delay_days: float = Field(ge=0)
    service_z: float = Field(ge=0)
    safety_stock_old: float = Field(ge=0)
    safety_stock_new: float = Field(ge=0)
    reorder_point_new: float = Field(ge=0)
    usable_stock: float = Field(ge=0)
    open_po_qty: float = Field(ge=0)
    net_requirement: float = Field(ge=0)


class TransferOrder(_Model):
    """Stock transfer between storage locations (S/4 movement 311) → FF_AG_TRANSFER."""

    transfer_id: str
    material: str
    plant: str
    from_location: str
    to_location: str
    qty: float = Field(gt=0)
    cover_to_before: float = Field(ge=0)
    cover_to_after: float = Field(ge=0)
    cover_from_after: float = Field(ge=0)
    cold_chain: bool = False
    decision_id: str | None = None
    status: str = "PLANNED"


class InventoryPlan(_Model):
    run_id: str
    safety_stock: list[SafetyStockUpdate] = Field(default_factory=list)
    transfers: list[TransferOrder] = Field(default_factory=list)
    procurement: list[Forecast] = Field(default_factory=list, description="LOGISTICS_DELAY forecasts handed to A4/A5")


# ---------------------------------------------------------------- Compliance & human-in-the-loop


class Autonomy(str, Enum):
    AUTO = "AUTO"
    HUMAN = "HUMAN"
    BLOCKED = "BLOCKED"


class DecisionStatus(str, Enum):
    EXECUTED = "EXECUTED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


class DecisionType(str, Enum):
    SIGNAL_TRIAGE = "SIGNAL_TRIAGE"
    SCENARIO_PLAN = "SCENARIO_PLAN"
    SHIPMENT_ROUTING = "SHIPMENT_ROUTING"
    STOCK_TRANSFER = "STOCK_TRANSFER"
    SAFETY_STOCK_UPDATE = "SAFETY_STOCK_UPDATE"
    PROCUREMENT_PROPOSAL = "PROCUREMENT_PROPOSAL"


class ComplianceCheck(_Model):
    check_id: str
    status: CheckStatus
    detail: str


class DecisionOption(_Model):
    option_id: str
    label: str
    cost_inr: Money | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Decision(_Model):
    """Every autonomous or human-gated decision, with rationale → FF_AG_DECISION (+ hash-chained audit row)."""

    decision_id: str
    run_id: str
    agent: str
    decision_type: DecisionType
    subject: str = Field(max_length=200)
    autonomy: Autonomy
    status: DecisionStatus
    rationale: str = Field(max_length=4000)
    options: list[DecisionOption] = Field(default_factory=list)
    recommended_option: str | None = None
    chosen_option: str | None = None
    cost_inr: Money | None = None
    checks: list[ComplianceCheck] = Field(default_factory=list)
    human_reasons: list[str] = Field(default_factory=list)
    data_sources: list[DataSourceRef] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None


class AuditEvent(_Model):
    """Append-only, hash-chained audit row → FF_AG_AUDIT_LOG (plan §3 A6)."""

    run_id: str
    seq: int = Field(ge=0)
    agent: str
    ts: datetime = Field(default_factory=_utcnow)
    input_hash: str
    output_hash: str
    rule_versions: dict[str, str] = Field(default_factory=dict)
    data_sources: list[DataSourceRef] = Field(default_factory=list)
    prev_hash: str | None = Field(default=None, description="None only for the first event of the chain")
    event_hash: str | None = None
