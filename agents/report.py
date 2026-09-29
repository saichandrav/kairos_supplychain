"""Audit & Compliance Report Generator (plan §3 A6, §8, NABH evidence).

Generates a standalone, print-ready HTML audit report for a specific procurement decision.
Contains:
1. Executive Decision Summary (Who, What, Why).
2. S/4HANA Purchase Requisition Details.
3. Pharmacist Manual Verification Checklist (CP-01 to CP-14).
4. Policy & Feasibility Validation (A5 checks).
5. Append-only Cryptographic Hash Chain Audit Trail.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from agents import a6_audit
from agents.common import try_query
from agents.ctx import Db


def generate_report(scenario_id: str, db: Db, run_id: str | None = None) -> str:
    """Generate self-contained HTML audit report for NABH compliance."""
    # 1. Fetch approval details
    if run_id:
        app_df = try_query(
            db,
            "SELECT * FROM FF_AG_APPROVAL WHERE RUN_ID = ? AND SCENARIO_ID = ? ORDER BY DECIDED_AT DESC LIMIT 1",
            (run_id, scenario_id)
        )
    else:
        app_df = try_query(
            db,
            "SELECT * FROM FF_AG_APPROVAL WHERE SCENARIO_ID = ? ORDER BY DECIDED_AT DESC LIMIT 1",
            (scenario_id,)
        )

    app = app_df.iloc[0].to_dict() if app_df is not None and not app_df.empty else None
    actual_run_id = app["RUN_ID"] if app else (run_id or "")

    # 2. Fetch scenario & recommendation
    scen_df = try_query(db, "SELECT * FROM FF_AG_SCENARIO WHERE SCENARIO_ID = ?", (scenario_id,))
    scen = scen_df.iloc[0].to_dict() if scen_df is not None and not scen_df.empty else {}

    rec_df = try_query(db, "SELECT * FROM FF_AG_RECOMMENDATION WHERE SCENARIO_ID = ?", (scenario_id,))
    rec = rec_df.iloc[0].to_dict() if rec_df is not None and not rec_df.empty else {}

    # 3. Fetch A5 checks
    chk_df = try_query(db, "SELECT * FROM FF_AG_CHECK WHERE SCENARIO_ID = ? ORDER BY STATUS DESC", (scenario_id,))
    checks = chk_df.to_dict(orient="records") if chk_df is not None and not chk_df.empty else []

    # 4. Fetch EBAN (PR) row
    eban_df = try_query(db, "SELECT * FROM FF_MM_EBAN WHERE SCENARIO_ID = ? ORDER BY BANFN DESC LIMIT 1", (scenario_id,))
    eban = eban_df.iloc[0].to_dict() if eban_df is not None and not eban_df.empty else {}

    # 5. Fetch audit trail and verify chain
    chain_ok, chain_err = a6_audit.verify_chain(actual_run_id, db)
    audit_df = try_query(
        db,
        "SELECT * FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ? ORDER BY SEQ ASC",
        (actual_run_id,)
    )
    audit_events = audit_df.to_dict(orient="records") if audit_df is not None and not audit_df.empty else []

    checklist = json.loads(app.get("CHECKLIST_JSON") or "{}") if app else {}

    # Build HTML
    chk_rows = ""
    for c in checks:
        badge_cls = "pass" if c["STATUS"] == "PASS" else "warn" if c["STATUS"] == "WARN" else "fail"
        ev = json.loads(c.get("EVIDENCE_JSON") or "{}")
        ev_summary = ", ".join(f"<strong>{k}:</strong> {v}" for k, v in ev.items() if k != "rule")
        chk_rows += f"""
        <tr>
            <td><code>{c['CHECK_ID']}</code></td>
            <td>{c['NAME']}</td>
            <td><span class="badge {badge_cls}">{c['STATUS']}</span></td>
            <td><small>{c.get('RULE_SOURCE') or '-'}</small></td>
            <td><small>{ev_summary}</small></td>
        </tr>
        """

    audit_rows = ""
    for a in audit_events:
        audit_rows += f"""
        <tr>
            <td>#{a['SEQ']}</td>
            <td><strong>{a['AGENT']}</strong></td>
            <td><small>{a['TS']}</small></td>
            <td><code>{a['INPUT_HASH'][:12]}...</code></td>
            <td><code>{a['OUTPUT_HASH'][:12]}...</code></td>
            <td><code>{a['ROW_HASH'][:12]}...</code></td>
        </tr>
        """

    chain_badge = '<span class="badge pass">VERIFIED (Tamper-Free)</span>' if chain_ok else f'<span class="badge fail">CORRUPTED: {chain_err}</span>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Kairos Audit Dossier - {scenario_id}</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 30px; color: #1e293b; background: #f8fafc; line-height: 1.5; }}
    .container {{ max-width: 960px; margin: 0 auto; background: #fff; padding: 40px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    h1 {{ font-size: 24px; color: #0f172a; margin-top: 0; border-bottom: 2px solid #e2e8f0; padding-bottom: 12px; }}
    h2 {{ font-size: 18px; color: #1e293b; margin-top: 28px; border-bottom: 1px solid #e2e8f0; padding-bottom: 6px; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
    .card {{ background: #f1f5f9; padding: 16px; border-radius: 6px; }}
    .badge {{ display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; text-transform: uppercase; }}
    .badge.pass {{ background: #dcfce7; color: #166534; }}
    .badge.warn {{ background: #fef9c3; color: #854d0e; }}
    .badge.fail {{ background: #fee2e2; color: #991b1b; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e2e8f0; }}
    th {{ background: #f8fafc; font-weight: 600; color: #475569; }}
    code {{ font-family: monospace; background: #e2e8f0; padding: 2px 4px; border-radius: 3px; font-size: 12px; }}
    .meta-item {{ margin-bottom: 8px; }}
    .meta-label {{ font-weight: 600; color: #64748b; font-size: 13px; }}
    .checklist-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }}
    .check-item {{ padding: 8px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px; font-size: 13px; }}
</style>
</head>
<body>
<div class="container">
    <h1>Kairos NABH Procurement Audit Dossier</h1>
    <div style="color: #64748b; margin-bottom: 20px;">
        Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')} &bull; Run ID: <code>{actual_run_id}</code>
    </div>

    <div class="grid">
        <div class="card">
            <h3>Decision Summary</h3>
            <div class="meta-item"><span class="meta-label">Scenario ID:</span> <code>{scenario_id}</code></div>
            <div class="meta-item"><span class="meta-label">Formulation:</span> {scen.get('FORM_ID', '-')}</div>
            <div class="meta-item"><span class="meta-label">Option Type:</span> <strong>{scen.get('OPTION_TYPE', '-')}</strong></div>
            <div class="meta-item"><span class="meta-label">Recommended Qty:</span> {scen.get('QTY', '-')}</div>
            <div class="meta-item"><span class="meta-label">Estimated Cost:</span> ₹{scen.get('COST_INR', '-')}</div>
        </div>

        <div class="card">
            <h3>Human Gate Verification</h3>
            <div class="meta-item"><span class="meta-label">Approver:</span> {app.get('APPROVER', 'Pending')}</div>
            <div class="meta-item"><span class="meta-label">Action:</span> <span class="badge pass">{app.get('ACTION', 'PENDING')}</span></div>
            <div class="meta-item"><span class="meta-label">Decided At:</span> {app.get('DECIDED_AT', '-')}</div>
            <div class="meta-item"><span class="meta-label">Reason:</span> {app.get('REASON') or 'Routine Proactive Buffer'}</div>
        </div>
    </div>

    <h2>SAP S/4HANA Purchase Requisition (Mock EBAN)</h2>
    <div class="card">
        <div class="grid">
            <div>
                <div class="meta-item"><span class="meta-label">PR Number (BANFN):</span> <code>{eban.get('BANFN', 'Not Yet Posted')}</code></div>
                <div class="meta-item"><span class="meta-label">Material (MATNR):</span> {eban.get('MATNR', '-')}</div>
                <div class="meta-item"><span class="meta-label">Plant (WERKS):</span> {eban.get('WERKS', '-')}</div>
            </div>
            <div>
                <div class="meta-item"><span class="meta-label">Approved Quantity:</span> {eban.get('MENGE', '-')} {eban.get('MEINS', '')}</div>
                <div class="meta-item"><span class="meta-label">Supplier (FLIEF):</span> {eban.get('FLIEF', '-')}</div>
                <div class="meta-item"><span class="meta-label">Target Delivery:</span> {eban.get('LFDAT', '-')}</div>
            </div>
        </div>
    </div>

    <h2>Chief Pharmacist Verification Checklist</h2>
    <div class="checklist-grid">
        <div class="check-item">{'[x]' if checklist.get('cp01_material_master') else '[ ]'} CP-01 Material master generic, strength & pack match</div>
        <div class="check-item">{'[x]' if checklist.get('cp02_rol_roq_stock') else '[ ]'} CP-02 ROL/ROQ buffer stock within safe limits</div>
        <div class="check-item">{'[x]' if checklist.get('cp04_supplier_verified') else '[ ]'} CP-04 Supplier licence, GST and performance verified</div>
        <div class="check-item">{'[x]' if checklist.get('price_within_ceiling_gst') else '[ ]'} Unit price &le; NPPA ceiling + GST confirmed</div>
        <div class="check-item">{'[x]' if checklist.get('shelf_life_fefo_ok') else '[ ]'} Shelf life &ge; 12 mo, no expiry waste risk</div>
        <div class="check-item">{'[x]' if checklist.get('cause_reviewed_not_claim') else '[ ]'} Probabilistic risk flag acknowledged (not manufacturer claim)</div>
    </div>

    <h2>A5 Policy & Feasibility Validation</h2>
    <table>
        <thead>
            <tr>
                <th>Check ID</th>
                <th>Rule Name</th>
                <th>Status</th>
                <th>Source</th>
                <th>Evidence</th>
            </tr>
        </thead>
        <tbody>
            {chk_rows or '<tr><td colspan="5">No automated check records found</td></tr>'}
        </tbody>
    </table>

    <h2>Cryptographic Audit Chain (A6 Observer) &bull; {chain_badge}</h2>
    <table>
        <thead>
            <tr>
                <th>Seq</th>
                <th>Agent Hop</th>
                <th>Timestamp (UTC)</th>
                <th>Input Hash</th>
                <th>Output Hash</th>
                <th>Chain Hash</th>
            </tr>
        </thead>
        <tbody>
            {audit_rows or '<tr><td colspan="6">No audit records found</td></tr>'}
        </tbody>
    </table>
</div>
</body>
</html>
"""
    return html
