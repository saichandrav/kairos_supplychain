"""Kairos REST API (plan §2, §7, §9, S09).

Serves SAP Build Apps and the local fallback UI:
- Health check
- Risk watchlist and molecule deep dive
- On-demand shock runs (e.g. +30% API cost)
- Human approval queue and decision submission
- S/4 Purchase Requisition posting via A6 Action
- Append-only audit log and HTML evidence report
- CDSCO NSQ recall trace across pharmacy inventory
"""
from __future__ import annotations

from collections import defaultdict
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from agents import (
    a6_audit, compliance, disruption_sensing, inventory_rebalance, live_sensing, logistics, orchestrator, report,
    scenario_planning, sea_routes, shipment, suppliers,
)
from agents.common import as_of_date, try_query
from agents.contracts import ApprovalAction, ApprovalChecklist, DisruptionSignalIn, WhatIfSpec
from agents.ctx import Db, Settings, get_db, load_settings
from agents.rules import load_rules

log = logging.getLogger(__name__)

UI_FILE = Path(__file__).resolve().parent.parent / "ui" / "fallback" / "index.html"
LOGO_FILE = Path(__file__).resolve().parent.parent / "ui" / "fallback" / "logo.png"
LANDING_FILE = Path(__file__).resolve().parent.parent / "ui" / "fallback" / "landing.html"

app = FastAPI(
    title="Kairos API",
    description="Early warning & human-gated resilience for price-controlled pharmaceuticals",
    version="1.0.0",
)

# Enable CORS for SAP Build Apps, local dev, and SAP BTP
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_current_settings() -> Settings:
    return load_settings()


def verify_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_current_settings),
) -> None:
    """Validate X-API-Key header against APP_API_KEY if configured."""
    expected = settings.app_api_key
    if expected:
        if not x_api_key or x_api_key.strip() != expected.strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-API-Key header",
            )


# ---------------------------------------------------------------- Models


class RunRequest(BaseModel):
    form_ids: list[str] = Field(default_factory=list, description="Target formulation IDs (empty = all)")
    shock: float = Field(default=1.0, description="API cost shock multiplier (e.g. 1.3 = +30%)")


class ShockRequest(BaseModel):
    multiplier: float = Field(default=1.3, description="Cost multiplier, e.g. 1.3 = +30%")


class ApprovalSubmission(BaseModel):
    scenario_id: str
    decision: ApprovalAction
    checklist: ApprovalChecklist = Field(default_factory=ApprovalChecklist)
    reason: str | None = None
    edited_qty: float | None = None
    edited_supplier: str | None = None
    snooze_days: int | None = None
    user: str = "chief_pharmacist@psg.edu"
    run_id: str | None = None


# ---------------------------------------------------------------- Routes


@app.get("/", include_in_schema=False)
def serve_landing() -> FileResponse:
    """Serve the plain-language landing page (nav bar + hamburger menu, links through to the dashboard)."""
    if LANDING_FILE.exists():
        return FileResponse(LANDING_FILE)
    raise HTTPException(status_code=404, detail="landing.html not found")


@app.get("/dashboard", include_in_schema=False)
def serve_ui() -> FileResponse:
    """Serve the interactive web UI fallback dashboard."""
    if UI_FILE.exists():
        return FileResponse(UI_FILE)
    raise HTTPException(status_code=404, detail="UI index.html not found")


@app.get("/logo.png", include_in_schema=False)
def serve_logo() -> FileResponse:
    """Serve the brand logo asset."""
    if LOGO_FILE.exists():
        return FileResponse(LOGO_FILE, media_type="image/png")
    raise HTTPException(status_code=404, detail="Logo not found")


@app.get("/health", tags=["System"])
def health(settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """Public health check endpoint for Cloud Foundry and load balancers."""
    return {
        "status": "healthy",
        "app": "Kairos",
        "version": "1.0.0",
        "backend": settings.db_backend,
    }


@app.get("/auth/config", tags=["System"])
def auth_config(settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """Provide web dashboard with API key configuration."""
    return {
        "auth_required": bool(settings.app_api_key),
        "api_key": settings.app_api_key or "",
    }


@app.get("/inventory", dependencies=[Depends(verify_api_key)], tags=["Inventory Hub"])
def get_inventory(settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """Return live Multidepot Medicine Inventory with batch expiry, FEFO calculations, and stock coverage."""
    db = get_db(settings)
    with db:
        sql = """
        SELECT 
            m.MATNR,
            m.MAKTX,
            m.GENERIC,
            m.STRENGTH,
            m.FORM_ID,
            m.VED,
            m.ABC,
            m.FSN,
            m.RAUBE,
            COALESCE(w.NAME1, d.WERKS, 'Central Hospital Pharmacy') AS WAREHOUSE,
            COALESCE(d.USABLE_QTY, 0) AS STOCK,
            COALESCE(d.UNEXPIRED_QTY, 0) AS UNEXPIRED_STOCK,
            ROUND(COALESCE(d.AVG_DAILY_ISSUE_90D, 0), 1) AS DAILY_BURN,
            ROUND(COALESCE(d.DAYS_OF_COVER, 0), 1) AS DAYS_COVER,
            d.NEXT_EXPIRY,
            f.THERAPEUTIC_CLASS,
            cp.CEILING_PRICE
        FROM FF_MM_MARA m
        LEFT JOIN FF_V_DAYS_OF_COVER d ON d.MATNR = m.MATNR
        LEFT JOIN FF_MM_T001W w ON w.WERKS = d.WERKS
        LEFT JOIN FF_REF_FORMULATION f ON f.FORM_ID = m.FORM_ID
        LEFT JOIN (
            SELECT FORM_ID, AVG(CEILING_PRICE) AS CEILING_PRICE
            FROM FF_REF_CEILING_PRICE
            GROUP BY FORM_ID
        ) cp ON cp.FORM_ID = m.FORM_ID
        ORDER BY d.DAYS_OF_COVER ASC
        """
        df = try_query(db, sql)
        if df is None or df.empty:
            return {"items": [], "summary": {}, "backend": settings.db_backend}

        batches_df = try_query(db, """
            SELECT MATNR, CHARG, LGORT, CLABS, USABLE_QTY, VFDAT, DAYS_TO_EXPIRY
            FROM FF_V_BATCH_FEFO
            ORDER BY MATNR, VFDAT ASC
        """)
        batches_by_matnr = defaultdict(list)
        if batches_df is not None and not batches_df.empty:
            for b in batches_df.where(batches_df.notna(), None).to_dict(orient="records"):
                batches_by_matnr[b["MATNR"]].append(b)

        supp_df = try_query(db, """
            SELECT p.MATNR, l.NAME1 AS SUPPLIER
            FROM FF_MM_EKPO p
            JOIN FF_MM_EKKO h ON h.EBELN = p.EBELN
            JOIN FF_MM_LFA1 l ON l.LIFNR = h.LIFNR
            GROUP BY p.MATNR, l.NAME1
        """)
        supplier_by_matnr = {}
        if supp_df is not None and not supp_df.empty:
            for s_row in supp_df.to_dict(orient="records"):
                supplier_by_matnr[s_row["MATNR"]] = s_row["SUPPLIER"]

        origin_df = try_query(db, """
            SELECT fa.FORM_ID, o.COUNTRY
            FROM FF_REF_FORM_API fa
            JOIN FF_REF_API_ORIGIN o ON o.API_ID = fa.API_ID
            GROUP BY fa.FORM_ID, o.COUNTRY
        """)
        origin_by_form = {}
        country_flags = {
            "CN": "China 🇨🇳",
            "IN": "India 🇮🇳",
            "DE": "Germany 🇩🇪",
            "US": "United States 🇺🇸",
            "CH": "Switzerland 🇨🇭",
            "JP": "Japan 🇯🇵",
            "IT": "Italy 🇮🇹",
            "KR": "South Korea 🇰🇷",
            "ES": "Spain 🇪🇸",
        }
        if origin_df is not None and not origin_df.empty:
            for o_row in origin_df.to_dict(orient="records"):
                c_code = o_row.get("COUNTRY")
                origin_by_form[o_row["FORM_ID"]] = country_flags.get(c_code, f"{c_code} 🌐" if c_code else "Global 🌐")

        items = []
        expiring_batches_90d = 0
        below_rop_count = 0
        sufficient_count = 0

        raw_items = df.where(df.notna(), None).to_dict(orient="records")
        for r in raw_items:
            matnr = r["MATNR"]
            daily_burn = float(r["DAILY_BURN"] or 0)
            usable_stock = float(r["STOCK"] or 0)
            unexpired_stock = float(r["UNEXPIRED_STOCK"] or 0)
            days_cover = float(r["DAYS_COVER"] or 0)
            rop = round(daily_burn * 30) if daily_burn > 0 else 500
            safety_stock = round(daily_burn * 15) if daily_burn > 0 else 250

            if usable_stock < rop:
                below_rop_count += 1

            if days_cover < 20:
                status_label = "Critical"
            elif days_cover < 45:
                status_label = "Low Stock"
            else:
                status_label = "Sufficient"
                sufficient_count += 1

            med_batches = batches_by_matnr.get(matnr, [])
            for b in med_batches:
                dte = b.get("DAYS_TO_EXPIRY")
                if dte is not None and dte <= 90:
                    expiring_batches_90d += 1

            if med_batches:
                nearest_batch = med_batches[0]
                n_date = str(nearest_batch.get("VFDAT") or "N/A")
                n_days = nearest_batch.get("DAYS_TO_EXPIRY")
                if n_days is not None:
                    if n_days <= 45:
                        fefo_cls = "fefo-exp"
                    elif n_days <= 90:
                        fefo_cls = "fefo-warn"
                    else:
                        fefo_cls = "fefo-ok"
                    label = f"{n_days}d"
                else:
                    fefo_cls = "fefo-ok"
                    label = "N/A"
                fefo = {"nearest": n_date, "label": label, "cls": fefo_cls}
            else:
                fefo = {"nearest": str(r.get("NEXT_EXPIRY") or "N/A"), "label": "N/A", "cls": "fefo-ok"}

            ceiling = float(r["CEILING_PRICE"]) if r.get("CEILING_PRICE") is not None else 18.5
            cost = round(ceiling * 1.08, 2)
            headroom = round(ceiling - cost, 2)
            name_clean = (r.get("MAKTX") or r.get("GENERIC") or matnr).title()

            item = {
                "id": matnr,
                "matnr": matnr,
                "name": name_clean,
                "generic": r.get("GENERIC") or "",
                "strength": r.get("STRENGTH") or "",
                "code": matnr,
                "formId": r.get("FORM_ID"),
                "category": r.get("THERAPEUTIC_CLASS") or f"VED {r.get('VED') or 'N/A'}",
                "warehouse": r.get("WAREHOUSE") or "Central Hospital Pharmacy",
                "stock": int(usable_stock),
                "unexpiredStock": int(unexpired_stock),
                "dailyBurn": daily_burn,
                "rop": rop,
                "safetyStock": safety_stock,
                "daysCover": days_cover,
                "status": status_label,
                "ceilingPrice": ceiling,
                "estCost": cost,
                "headroom": headroom,
                "supplier": supplier_by_matnr.get(matnr, "Primary Hospital Supplier"),
                "origin": origin_by_form.get(r.get("FORM_ID"), "India 🇮🇳"),
                "fefo": fefo,
                "batches": med_batches,
                "storageCondition": r.get("RAUBE") or "AMBIENT",
                "ved": r.get("VED") or "",
                "abc": r.get("ABC") or "",
                "fsn": r.get("FSN") or "",
            }
            items.append(item)

        summary = {
            "totalMedicines": len(items),
            "expiringBatches90d": expiring_batches_90d,
            "belowRopCount": below_rop_count,
            "sufficientStockCount": sufficient_count,
            "backend": "SAP HANA Cloud" if settings.db_backend == "hana" else "SQLite (Local)",
            "schema": settings.hana_schema if settings.db_backend == "hana" else "main",
            "host": settings.hana_host if settings.db_backend == "hana" else "localhost",
        }

        return {"items": items, "summary": summary}


@app.get("/watchlist", dependencies=[Depends(verify_api_key)], tags=["Pharmacy Watchlist"])
def get_watchlist() -> list[dict[str, Any]]:
    """Return hospital-tracked formulations ranked by exit risk and exposure."""
    db = get_db()
    with db:
        df = try_query(db, "SELECT * FROM FF_V_WATCHLIST ORDER BY CASE WHEN EXIT_RISK_BAND = 'RED' THEN 1 WHEN EXIT_RISK_BAND = 'AMBER' THEN 2 ELSE 3 END, EXIT_RISK DESC, FORM_ID ASC")
        if df is None or df.empty:
            df = try_query(db, "SELECT * FROM FF_REF_FORMULATION LIMIT 50")
            if df is None:
                return []
        # Convert NaN to None for clean JSON serialization
        records = df.where(df.notna(), None).to_dict(orient="records")
        return records


@app.get("/molecule/{form_id}", dependencies=[Depends(verify_api_key)], tags=["Molecule Detail"])
def get_molecule(form_id: str) -> dict[str, Any]:
    """Deep-dive fact sheet for one formulation: signal, graph dependency, forecast, scenarios, and checks."""
    db = get_db()
    with db:
        # Reference formulation
        f_df = try_query(db, "SELECT * FROM FF_REF_FORMULATION WHERE FORM_ID = ?", (form_id,))
        form_info = f_df.iloc[0].to_dict() if f_df is not None and not f_df.empty else {"form_id": form_id}

        # Latest Signal
        s_df = try_query(db, "SELECT * FROM FF_AG_SIGNAL WHERE FORM_ID = ? ORDER BY CREATED_AT DESC LIMIT 1", (form_id,))
        signal = s_df.iloc[0].to_dict() if s_df is not None and not s_df.empty else None

        # Latest Dependency
        d_df = try_query(db, "SELECT * FROM FF_AG_DEPENDENCY WHERE FORM_ID = ? ORDER BY RUN_ID DESC LIMIT 1", (form_id,))
        dependency = d_df.iloc[0].to_dict() if d_df is not None and not d_df.empty else None

        # Latest Forecast
        fc_df = try_query(db, "SELECT * FROM FF_AG_FORECAST WHERE FORM_ID = ? ORDER BY CREATED_AT DESC LIMIT 1", (form_id,))
        forecast = fc_df.iloc[0].to_dict() if fc_df is not None and not fc_df.empty else None

        # Scenarios
        sc_df = try_query(db, "SELECT * FROM FF_AG_SCENARIO WHERE FORM_ID = ? ORDER BY RANK_NO ASC", (form_id,))
        scenarios = sc_df.where(sc_df.notna(), None).to_dict(orient="records") if sc_df is not None and not sc_df.empty else []

        # Graph neighbourhood (vertices & edges)
        edges_df = try_query(db, "SELECT * FROM FF_G_E WHERE SRC = ? OR DST = ? LIMIT 100", (form_id, form_id))
        edges = edges_df.to_dict(orient="records") if edges_df is not None and not edges_df.empty else []

        # Executive briefing (S11)
        briefing = None
        if forecast:
            from agents import explain
            briefing = explain.explain(forecast, dependency)

        return {
            "formulation": form_info,
            "signal": signal,
            "dependency": dependency,
            "forecast": forecast,
            "scenarios": scenarios,
            "graph": {"edges": edges},
            "explanation": briefing,
        }


@app.post("/run", dependencies=[Depends(verify_api_key)], tags=["Agent Execution"])
def run_pipeline(req: RunRequest) -> dict[str, Any]:
    """Trigger an on-demand multi-agent run for target formulations."""
    db = get_db()
    with db:
        res = orchestrator.run(
            form_ids=req.form_ids or None,
            shock=req.shock,
            ctx=db,
            dry_run=False,
        )
    return {
        "run_id": res["run_id"],
        "status": res["status"],
        "shock": res["shock"],
        "signals_count": len(res["signals"]),
        "forecasts_count": len(res["forecasts"]),
        "scenarios_count": len(res["scenarios"]),
        "recommendations_count": len(res["recommendations"]),
    }


@app.post("/scenario/shock", dependencies=[Depends(verify_api_key)], tags=["Agent Execution"])
def shock_scenario(req: ShockRequest) -> dict[str, Any]:
    """Execute shock scenario (e.g. Hormuz strait disruption +30% API cost)."""
    db = get_db()
    with db:
        res = orchestrator.run(shock=req.multiplier, ctx=db, dry_run=False)
    return {
        "run_id": res["run_id"],
        "status": res["status"],
        "shock": req.multiplier,
        "signals_count": len(res["signals"]),
        "recommendations_count": len(res["recommendations"]),
    }


@app.get("/approvals/pending", dependencies=[Depends(verify_api_key)], tags=["Human Approval Gate"])
def pending_approvals() -> list[dict[str, Any]]:
    """Return all recommendations requiring Chief Pharmacist verification that haven't been decided yet."""
    db = get_db()
    with db:
        sql = """
        SELECT r.RUN_ID, r.SCENARIO_ID, r.FORM_ID, r.OVERALL, r.NEEDS_SECOND_APPROVER,
               r.DRAFT_PR_JSON, s.OPTION_TYPE, s.SUPPLIER, s.QTY, s.UNIT_RATE_INR, s.COST_INR,
               s.COVERAGE_DAYS, s.EXPIRY_WASTE_RISK
        FROM FF_AG_RECOMMENDATION r
        JOIN FF_AG_SCENARIO s ON s.RUN_ID = r.RUN_ID AND s.SCENARIO_ID = r.SCENARIO_ID
        LEFT JOIN FF_AG_APPROVAL a ON a.RUN_ID = r.RUN_ID AND a.SCENARIO_ID = r.SCENARIO_ID
        WHERE r.OVERALL IN ('READY', 'NEEDS_CHANGES') AND a.ACTION IS NULL
        ORDER BY r.CREATED_AT DESC
        LIMIT 50
        """
        df = try_query(db, sql)
        if df is None or df.empty:
            return []
        records = df.where(df.notna(), None).to_dict(orient="records")
        for rec in records:
            if rec.get("DRAFT_PR_JSON"):
                try:
                    rec["draft_pr"] = json.loads(rec["DRAFT_PR_JSON"])
                except Exception:
                    pass
        return records


@app.post("/approval", dependencies=[Depends(verify_api_key)], tags=["Human Approval Gate"])
def submit_approval(sub: ApprovalSubmission) -> dict[str, Any]:
    """Submit human gate decision. If approved, A6 generates and posts S/4 Purchase Requisition."""
    db = get_db()
    with db:
        try:
            res = orchestrator.decide(
                scenario_id=sub.scenario_id,
                user=sub.user,
                action=sub.decision,
                checklist=sub.checklist,
                run_id=sub.run_id,
                reason=sub.reason,
                edited_qty=sub.edited_qty,
                edited_supplier=sub.edited_supplier,
                snooze_days=sub.snooze_days,
                db=db,
            )
            return res
        except PermissionError as pe:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(pe))
        except ValueError as ve:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(ve))


@app.get("/audit/{run_id}", dependencies=[Depends(verify_api_key)], tags=["Audit & Compliance"])
def get_audit(run_id: str) -> dict[str, Any]:
    """Return append-only hash chain audit trail and cryptographic integrity verification."""
    db = get_db()
    with db:
        valid, err = a6_audit.verify_chain(run_id, db)
        df = try_query(db, "SELECT * FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ? ORDER BY SEQ ASC", (run_id,))
        records = df.to_dict(orient="records") if df is not None and not df.empty else []
        return {
            "run_id": run_id,
            "chain_valid": valid,
            "chain_error": err,
            "event_count": len(records),
            "events": records,
        }


@app.get("/audit/{scenario_id}/report", response_class=HTMLResponse, dependencies=[Depends(verify_api_key)], tags=["Audit & Compliance"])
def get_audit_report(scenario_id: str) -> HTMLResponse:
    """Generate NABH-ready printable HTML procurement audit dossier."""
    db = get_db()
    with db:
        html = report.generate_report(scenario_id, db)
        return HTMLResponse(content=html, status_code=200)


@app.get("/recall/{alert_id}", dependencies=[Depends(verify_api_key)], tags=["CDSCO Recall Trace"])
def get_recall_trace(alert_id: str) -> list[dict[str, Any]]:
    """Trace CDSCO NSQ alert batch across pharmacy locations and inventory."""
    db = get_db()
    with db:
        if alert_id.lower() in ("all", "*"):
            df = try_query(db, "SELECT * FROM FF_V_RECALL_TRACE LIMIT 50")
        else:
            df = try_query(db, "SELECT * FROM FF_V_RECALL_TRACE WHERE ALERT_ID = ?", (alert_id,))
        if df is None or df.empty:
            return []
        return df.where(df.notna(), None).to_dict(orient="records")


# ---------------------------------------------------------------- Disruption Sensing


class DisruptionSignalRequest(BaseModel):
    signal_id: str = Field(description="Unique identifier for this signal")
    raw_text: str = Field(max_length=10000, description="The external signal text to classify")
    source: str = Field(description="Origin system or feed name")
    source_url: str | None = None
    context: dict[str, Any] = Field(default_factory=dict, description="Additional context")


@app.post("/disruption/assess", dependencies=[Depends(verify_api_key)], tags=["Disruption Sensing"])
async def assess_disruption(
    req: DisruptionSignalRequest,
    settings: Settings = Depends(get_current_settings),
) -> dict[str, Any]:
    """Classify and assess an incoming supply-chain disruption signal using AI (OpenRouter).

    The signal is interpreted but no autonomous action is taken.
    High-severity or ambiguous signals are flagged for human review.
    """
    signal = DisruptionSignalIn(
        signal_id=req.signal_id,
        raw_text=req.raw_text,
        source=req.source,
        source_url=req.source_url,
        context=req.context,
    )
    db = get_db(settings)
    assessment = await disruption_sensing.assess(signal, settings=settings, db=db)
    return assessment.model_dump(mode="json")


@app.get("/disruption/live", dependencies=[Depends(verify_api_key)], tags=["Disruption Sensing"])
async def disruption_live(settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """Real current marine weather (Open-Meteo, no key) + maritime/trade news (NewsAPI.org, needs
    NEWSAPI_KEY) for every mapped chokepoint and Indian port, classified into blocked/risky/congested
    by fixed keyword/threshold rules (not a model). `news_enabled` is false if NEWSAPI_KEY is unset -
    weather still works. Every finding carries its source reading or headline+url.
    """
    rules = load_rules()
    return await live_sensing.scan(rules, settings)


@app.get("/disruption/sea-routes", dependencies=[Depends(verify_api_key)], tags=["Disruption Sensing"])
def list_sea_routes() -> dict[str, Any]:
    """Return all pharmaceutical sea routes, chokepoints, and Indian ports from rules/sea_routes.yaml."""
    from agents.disruption_sensing import load_sea_routes
    cfg = load_sea_routes()
    return {
        "routes": cfg.get("routes", []),
        "chokepoints": cfg.get("chokepoints", {}),
        "indian_ports": cfg.get("indian_ports", {}),
    }


@app.get("/disruption/sea-routes/check", dependencies=[Depends(verify_api_key)], tags=["Disruption Sensing"])
def check_sea_route_exposure(
    location: str = Query(description="Location or chokepoint to check, e.g. 'Suez Canal', 'Bab el-Mandeb'"),
    settings: Settings = Depends(get_current_settings),
) -> dict[str, Any]:
    """Check which sea routes and formulations are exposed if a location/chokepoint is disrupted."""
    from agents.disruption_sensing import find_affected_routes, _build_sea_route_context
    from agents.contracts import AffectedEntity

    entities = [AffectedEntity(entity_type="location", entity_name=location, confidence=1.0)]
    matched = find_affected_routes(location, entities)
    db = get_db(settings)
    route_models, context_text = _build_sea_route_context(matched, db)
    return {
        "location_queried": location,
        "affected_routes_count": len(route_models),
        "routes": [r.model_dump(mode="json") for r in route_models],
        "total_affected_formulations": len({f for r in route_models for f in r.affected_formulations}),
        "summary": context_text or "No matching routes found.",
    }


# ---------------------------------------------------------------- Disruption response (multi-agent)

DEMO_FEED = Path(__file__).resolve().parent.parent / "data" / "demo" / "disruption_feed.json"


class FeedScanRequest(BaseModel):
    signals: list[DisruptionSignalRequest] = Field(default_factory=list, max_length=50,
                                                   description="Empty = scan the bundled SYNTH demo feed")


class WhatIfRequest(BaseModel):
    question: str | None = Field(default=None, max_length=2000, description="Free-text what-if, parsed by the LLM")
    specs: list[WhatIfSpec] = Field(default_factory=list, max_length=6)


class ExecuteRequest(BaseModel):
    specs: list[WhatIfSpec] = Field(default_factory=list, max_length=6, description="Empty = the saved plan's specs")


class ResolveRequest(BaseModel):
    approver: str = Field(min_length=3, max_length=120, description="Human decision-maker identity")
    approve: bool
    option_id: str | None = Field(default=None, max_length=100)
    reason: str | None = Field(default=None, max_length=1000)


def _signal(req: DisruptionSignalRequest) -> DisruptionSignalIn:
    return DisruptionSignalIn(signal_id=req.signal_id, raw_text=req.raw_text, source=req.source,
                              source_url=req.source_url, context=req.context)


@app.post("/disruption/respond", dependencies=[Depends(verify_api_key)], tags=["Disruption Response"])
async def disruption_respond(req: DisruptionSignalRequest,
                             settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """Full agentic response to a signal: Sensing -> Scenario Planning -> Logistics -> Inventory -> A4/A5 -> Compliance.

    Ambiguous signals stop at a human triage decision; gated actions wait in the decision queue.
    """
    with get_db(settings) as db:
        return await orchestrator.respond(_signal(req), settings, db)


@app.post("/disruption/feed/scan", dependencies=[Depends(verify_api_key)], tags=["Disruption Response"])
async def disruption_scan(req: FeedScanRequest | None = None,
                          settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """Monitoring pass over a batch of signals (default: the SYNTH demo feed). Already-seen signal ids are skipped."""
    if req and req.signals:
        signals = [_signal(s) for s in req.signals]
    else:
        feed = json.loads(DEMO_FEED.read_text(encoding="utf-8"))
        signals = [DisruptionSignalIn(**s) for s in feed["signals"]]
    with get_db(settings) as db:
        return {"results": await orchestrator.scan_feed(signals, settings, db)}


@app.get("/disruption/signals", dependencies=[Depends(verify_api_key)], tags=["Disruption Response"])
def disruption_signals(settings: Settings = Depends(get_current_settings)) -> list[dict[str, Any]]:
    """Recently assessed signals with their status (ESCALATED / MONITORING / PLANNED / DISMISSED)."""
    with get_db(settings) as db:
        return disruption_sensing.recent(db)


@app.post("/disruption/whatif", dependencies=[Depends(verify_api_key)], tags=["Scenario Planning"])
async def disruption_whatif(req: WhatIfRequest, settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """Simulate what-if disruptions (free text or explicit specs) and rank strategies. Takes no action."""
    rules = load_rules()
    with get_db(settings) as db:
        specs, used_llm, note = list(req.specs), False, None
        if not specs and req.question:
            specs, used_llm, note = await scenario_planning.parse_whatif(req.question, settings, db, rules)
        try:
            specs = scenario_planning.normalise(specs, db)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        if not specs:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="No disruption recognised. Name a chokepoint, Indian port, origin country, "
                                       "tariff or supplier.")
        plan = await orchestrator.simulate_whatif(specs, settings, db, rules)
    return {"plan": plan.model_dump(mode="json"), "parsed_by_llm": used_llm, "parse_note": note}


@app.get("/disruption/whatif", dependencies=[Depends(verify_api_key)], tags=["Scenario Planning"])
def disruption_whatif_list(settings: Settings = Depends(get_current_settings)) -> list[dict[str, Any]]:
    with get_db(settings) as db:
        return scenario_planning.recent_plans(db)


@app.post("/disruption/whatif/{plan_id}/execute", dependencies=[Depends(verify_api_key)], tags=["Scenario Planning"])
async def disruption_whatif_execute(plan_id: str, req: ExecuteRequest | None = None,
                                    settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """Turn a simulated plan into a live response run (autonomous within policy, the rest to humans)."""
    with get_db(settings) as db:
        plan = scenario_planning.load_plan(plan_id, db)
        if plan is None:
            raise HTTPException(status_code=404, detail=f"plan {plan_id} not found")
        specs = (req.specs if req and req.specs else plan.specs)
        return await orchestrator.execute_response(specs, settings, db, signal_id=plan.signal_id)


@app.get("/disruption/runs", dependencies=[Depends(verify_api_key)], tags=["Disruption Response"])
def disruption_runs(settings: Settings = Depends(get_current_settings)) -> list[dict[str, Any]]:
    with get_db(settings) as db:
        df = try_query(db, "SELECT r.RUN_ID, r.STARTED_AT, r.FINISHED_AT, r.STATUS, r.SHOCK_JSON, d.SIGNAL_ID, "
                           "d.EVENT_TYPE, d.SEVERITY, d.SUMMARY FROM FF_AG_RUN r LEFT JOIN FF_AG_DISRUPTION d "
                           "ON d.RUN_ID = r.RUN_ID WHERE r.TRIGGER_TYPE = 'DISRUPTION' "
                           "ORDER BY r.STARTED_AT DESC LIMIT 50")
        return [] if df is None or df.empty else json.loads(df.to_json(orient="records", date_format="iso"))


@app.get("/disruption/runs/{run_id}", dependencies=[Depends(verify_api_key)], tags=["Disruption Response"])
def disruption_run_detail(run_id: str, settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """Everything a run did: plan, re-planned shipments, transfers, safety stock, notices, decisions, chain check."""
    with get_db(settings) as db:
        try:
            rep = compliance.report(run_id, db)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {**rep, "shipments": logistics.shipments(db, run_id), "transfers": inventory_rebalance.transfers(db, run_id),
                "safety_stock": inventory_rebalance.safety_stock(db, run_id),
                "notifications": logistics.notifications(db, run_id)}


@app.get("/disruption/runs/{run_id}/report", response_class=HTMLResponse, dependencies=[Depends(verify_api_key)],
         tags=["Audit & Compliance"])
def disruption_run_report(run_id: str, settings: Settings = Depends(get_current_settings)) -> HTMLResponse:
    """Audit-ready printable dossier for a disruption response run."""
    with get_db(settings) as db:
        try:
            rep = compliance.report(run_id, db)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        html = compliance.report_html(rep, logistics.shipments(db, run_id), inventory_rebalance.transfers(db, run_id),
                                      logistics.notifications(db, run_id))
    return HTMLResponse(content=html)


@app.get("/disruption/decisions", dependencies=[Depends(verify_api_key)], tags=["Human-in-the-Loop"])
def disruption_decisions(status_filter: str | None = Query(default=None, alias="status"), run_id: str | None = None,
                         settings: Settings = Depends(get_current_settings)) -> list[dict[str, Any]]:
    """Decision log (autonomous and human). ?status=PENDING is the human decision queue."""
    with get_db(settings) as db:
        return [d.model_dump(mode="json") for d in compliance.decisions(db, run_id=run_id, status=status_filter)]


@app.post("/disruption/decisions/{decision_id}/resolve", dependencies=[Depends(verify_api_key)],
          tags=["Human-in-the-Loop"])
async def disruption_resolve(decision_id: str, req: ResolveRequest,
                             settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """A human approves (optionally picking another option) or rejects a pending decision; the owning agent executes."""
    with get_db(settings) as db:
        try:
            return await orchestrator.resolve_decision(decision_id, req.approver, req.approve, req.option_id,
                                                       req.reason, settings, db)
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@app.get("/disruption/shipments", dependencies=[Depends(verify_api_key)], tags=["Rerouting & Logistics"])
def disruption_shipments(settings: Settings = Depends(get_current_settings)) -> list[dict[str, Any]]:
    """Latest re-planned inbound PO lines across runs."""
    with get_db(settings) as db:
        return logistics.shipments(db)[:100]


# ---------------------------------------------------------------- Route Optimizer (quote & route)


def _route_json(q: "shipment.RouteQuote | None") -> dict[str, Any] | None:
    if q is None:
        return None
    return {
        "option_id": q.option_id, "label": q.label, "kind": q.kind, "mode": q.mode, "carrier": q.carrier,
        "via": list(q.via), "transit_days": q.transit_days, "extra_days": q.extra_days, "eta": q.eta.isoformat(),
        "freight_cost_inr": float(q.freight_cost_inr), "cost_per_unit_inr": float(q.cost_per_unit_inr),
        "security_risk": q.security_risk, "recommended": q.recommended,
        "origin_port": q.origin_port, "destination_port": q.destination_port,
        "distance_km": q.distance_km, "geometry": q.geometry,
    }


class ShipmentQuoteRequest(BaseModel):
    origin: str = Field(description="Origin country code, e.g. CN, DE, US")
    qty: float = Field(gt=0, description="Consignment quantity (units)")
    unit_value_inr: float = Field(gt=0, description="Value per unit, INR")
    destination_port: str | None = Field(default=None, description="Preferred Indian discharge port")
    blocked_chokepoints: list[str] = Field(default_factory=list, description="Simulate closure, e.g. 'Suez Canal'")
    risky_chokepoints: list[str] = Field(default_factory=list, description="Simulate elevated security risk")
    blocked_ports: list[str] = Field(default_factory=list, description="Simulate a closed Indian port")
    congested_ports: list[str] = Field(default_factory=list, description="Simulate port congestion")
    disruption_duration_days: int = Field(default=30, ge=1, le=365)


@app.get("/shipment/ports", dependencies=[Depends(verify_api_key)], tags=["Route Optimizer"])
def shipment_ports() -> dict[str, Any]:
    """Origin countries/ports and Indian destination ports (with coordinates) for the route form and map."""
    return {"origins": shipment.origin_summary(), "destinations": shipment.destination_summary()}


@app.post("/shipment/quote", dependencies=[Depends(verify_api_key)], tags=["Route Optimizer"])
def shipment_quote(req: ShipmentQuoteRequest, settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """Price, ETA and map every sea/air option for a new consignment; recommend the cheapest open, safe one.

    Uses the same lane options and cost rules as Rerouting & Logistics. Optional blocked/risky
    chokepoint or port lists overlay a hypothetical disruption so recommendations update live as
    a what-if is toggled. This only returns a quote; it books nothing and needs no approval.
    """
    if req.origin not in sea_routes.origins():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Unknown origin '{req.origin}'. Known origins: {', '.join(sea_routes.origins())}")
    rules = load_rules()
    with get_db(settings) as db:
        as_of = as_of_date(db)
    state = sea_routes.LaneState(
        blocked_chokepoints=set(req.blocked_chokepoints), risky_chokepoints=set(req.risky_chokepoints),
        blocked_ports=set(req.blocked_ports), congested_ports=set(req.congested_ports),
        duration_days=req.disruption_duration_days)
    quotes = shipment.quote(req.origin, req.qty, req.unit_value_inr, as_of, rules, state, req.destination_port)
    if not quotes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="No open route for this origin under the given disruption.")
    destination_note = None
    if req.destination_port and not any(q.destination_port == req.destination_port for q in quotes):
        served = shipment.destination_ports_for(req.origin)
        destination_note = (f"No mapped lane from {sea_routes.origin_name(req.origin)} discharges at "
                            f"{req.destination_port}; showing the nearest port it actually reaches instead "
                            f"({', '.join(served)}).")
    return {
        "origin": req.origin, "origin_name": sea_routes.origin_name(req.origin),
        "qty": req.qty, "unit_value_inr": req.unit_value_inr, "value_inr": req.qty * req.unit_value_inr,
        "as_of": as_of.isoformat(), "destination_note": destination_note,
        "options": [_route_json(q) for q in quotes],
    }


# ---------------------------------------------------------------- Order a Medicine (AI supplier pick)


class SupplierRankRequest(BaseModel):
    medicine_id: str = Field(description="Seeded demo medicine id, from GET /suppliers/medicines")
    qty: float = Field(gt=0, description="Order quantity (units)")
    blocked_chokepoints: list[str] = Field(default_factory=list, description="Simulate closure, e.g. 'Suez Canal'")
    risky_chokepoints: list[str] = Field(default_factory=list, description="Simulate elevated security risk")
    blocked_ports: list[str] = Field(default_factory=list, description="Simulate a closed Indian port")
    congested_ports: list[str] = Field(default_factory=list, description="Simulate port congestion")


@app.get("/suppliers/medicines", dependencies=[Depends(verify_api_key)], tags=["Route Optimizer"])
def list_demo_medicines() -> list[dict[str, Any]]:
    """Seeded demo medicines with a supplier panel, for the 'Order a Medicine' picker."""
    return suppliers.medicines()


@app.post("/suppliers/rank", dependencies=[Depends(verify_api_key)], tags=["Route Optimizer"])
def rank_demo_suppliers(req: SupplierRankRequest, settings: Settings = Depends(get_current_settings)) -> dict[str, Any]:
    """Rank every seeded supplier for a medicine by risk-adjusted landed cost (price x qty + freight,
    divided by reliability) and recommend the best one, with each candidate's route for the map.

    Suppliers are a SYNTH demo panel (data/demo/suppliers.json), not a live vendor feed. This only
    returns a ranking; it books nothing and needs no approval.
    """
    rules = load_rules()
    with get_db(settings) as db:
        as_of = as_of_date(db)
    state = sea_routes.LaneState(blocked_chokepoints=set(req.blocked_chokepoints),
                                 risky_chokepoints=set(req.risky_chokepoints),
                                 blocked_ports=set(req.blocked_ports), congested_ports=set(req.congested_ports))
    result = suppliers.rank_suppliers(req.medicine_id, req.qty, as_of, rules, state)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"No demo suppliers seeded for medicine '{req.medicine_id}'")
    return {
        "medicine_id": result["medicine_id"], "name": result["name"], "generic": result["generic"],
        "qty": result["qty"], "source": result["source"], "is_proxy": result["is_proxy"], "as_of": as_of.isoformat(),
        "offers": [{
            "supplier_id": o.supplier_id, "name": o.name, "country": o.country, "country_name": o.country_name,
            "city": o.city, "unit_price_inr": float(o.unit_price_inr), "reliability": o.reliability,
            "lead_time_days": o.lead_time_days, "certification": o.certification,
            "landed_cost_inr": float(o.landed_cost_inr), "total_days": o.total_days, "recommended": o.recommended,
            "route": _route_json(o.route),
        } for o in result["offers"]],
    }
