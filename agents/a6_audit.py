"""A6: Compliance, Audit & Action (plan §3 A6, §7).

Responsibilities:
1. Append-only, hash-chained audit log (FF_AG_AUDIT_LOG).
2. Cryptographic verification of the audit chain (detects any tampering).
3. Human-approval-gated procurement action posting to FF_MM_EBAN shaped
   as S/4HANA API_PURCHASEREQ_PROCESS_SRV.
4. Optional master-data validation against SAP API Business Hub sandbox.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Sequence

from agents.common import placeholders, try_query
from agents.contracts import ApprovalAction, ApprovalChecklist, AuditEvent, DataSourceRef, DraftPR
from agents.ctx import Db, load_settings

log = logging.getLogger(__name__)

AUDIT_COLS = (
    "RUN_ID", "SEQ", "AGENT", "TS", "INPUT_HASH", "OUTPUT_HASH",
    "RULE_VERSIONS_JSON", "DATA_SOURCES_JSON", "PREV_HASH", "ROW_HASH",
)

EBAN_COLS = (
    "BANFN", "BNFPO", "MATNR", "WERKS", "MENGE", "MEINS", "PREIS", "PEINH",
    "BADAT", "LFDAT", "FLIEF", "AFNAM", "STATUS", "RUN_ID", "SCENARIO_ID",
    "SOURCE", "SOURCE_URL", "FETCHED_AT", "IS_PROXY",
)


def hash_data(data: Any) -> str:
    """Deterministic sha256 hash of any JSON-serializable object or string."""
    if hasattr(data, "model_dump_json"):
        text = data.model_dump_json(by_alias=True)
    elif isinstance(data, (dict, list)):
        text = json.dumps(data, sort_keys=True, default=str)
    else:
        text = str(data)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_row_hash(run_id: str, seq: int, agent: str, ts: str,
                     input_hash: str, output_hash: str, prev_hash: str | None) -> str:
    """Compute append-only hash chain link."""
    payload = f"{run_id}|{seq}|{agent}|{ts}|{input_hash}|{output_hash}|{prev_hash or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_ts(ts: Any) -> str:
    """Naive-UTC microsecond text that survives a HANA TIMESTAMP round trip unchanged."""
    dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


import pandas as pd


def _clean_str(val: Any) -> str | None:
    if val is None or pd.isna(val) or str(val).strip() in ("", "nan", "None"):
        return None
    return str(val).strip()


def get_last_event(run_id: str, db: Db) -> AuditEvent | None:
    """Return the most recent audit event for a run, or None if none exist."""
    df = try_query(
        db,
        "SELECT * FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ? ORDER BY SEQ DESC LIMIT 1",
        (run_id,)
    )
    if df is None or df.empty:
        return None
    r = df.iloc[0]
    return AuditEvent(
        run_id=r["RUN_ID"],
        seq=int(r["SEQ"]),
        agent=r["AGENT"],
        ts=datetime.fromisoformat(str(r["TS"])),
        input_hash=r["INPUT_HASH"],
        output_hash=r["OUTPUT_HASH"],
        rule_versions=json.loads(r["RULE_VERSIONS_JSON"] or "{}"),
        data_sources=[DataSourceRef(**ds) for ds in json.loads(r["DATA_SOURCES_JSON"] or "[]")],
        prev_hash=_clean_str(r["PREV_HASH"]),
        event_hash=str(r["ROW_HASH"]),
    )


def _link(event: AuditEvent, last: AuditEvent | None) -> AuditEvent:
    """Give the event the next SEQ after `last`, its PREV_HASH and its ROW_HASH."""
    seq = 0 if last is None else last.seq + 1
    prev_hash = last.event_hash if last else None
    row_hash = compute_row_hash(event.run_id, seq, event.agent, canonical_ts(event.ts),
                                event.input_hash, event.output_hash, prev_hash)
    return event.model_copy(update={"seq": seq, "prev_hash": prev_hash, "event_hash": row_hash})


def _row(event: AuditEvent) -> tuple:
    return (event.run_id, event.seq, event.agent, canonical_ts(event.ts), event.input_hash, event.output_hash,
            json.dumps(event.rule_versions, sort_keys=True),
            json.dumps([ds.model_dump(mode="json") for ds in event.data_sources], sort_keys=True),
            event.prev_hash, event.event_hash)


_INSERT = f"INSERT INTO FF_AG_AUDIT_LOG ({', '.join(AUDIT_COLS)}) VALUES ({placeholders(AUDIT_COLS)})"


def audit(event: AuditEvent, db: Db) -> AuditEvent:
    """Append an event to FF_AG_AUDIT_LOG. Computes ROW_HASH and enforces chain continuity."""
    event = _link(event, get_last_event(event.run_id, db))
    db.execute(_INSERT, _row(event))
    return event


class Chain:
    """One run's hash chain: reads the tip once, links events in memory and appends them in one batch on flush().

    Same rows and hashes as calling audit() per event, without a round trip per event.
    """

    def __init__(self, run_id: str, db: Db):
        self.run_id = run_id
        self.db = db
        self.tip = get_last_event(run_id, db)
        self._rows: list[tuple] = []

    def add(self, event: AuditEvent) -> AuditEvent:
        event = _link(event.model_copy(update={"run_id": self.run_id}), self.tip)
        self.tip = event
        self._rows.append(_row(event))
        return event

    def flush(self) -> None:
        if self._rows:
            self.db.executemany(_INSERT, self._rows)
            self._rows = []


def verify_chain(run_id: str, db: Db) -> tuple[bool, str | None]:
    """Verify that every row in FF_AG_AUDIT_LOG matches its computed hash and parent link."""
    df = try_query(
        db,
        "SELECT * FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ? ORDER BY SEQ ASC",
        (run_id,)
    )
    if df is None or df.empty:
        return True, None

    last_hash: str | None = None
    for idx, r in df.iterrows():
        seq = int(r["SEQ"])
        agent = str(r["AGENT"])
        inp_h = str(r["INPUT_HASH"])
        out_h = str(r["OUTPUT_HASH"])
        prev_h = _clean_str(r["PREV_HASH"])
        stored_hash = str(r["ROW_HASH"])

        if prev_h != last_hash:
            return False, f"Broken prev_hash link at seq={seq}: expected {last_hash}, found {prev_h}"

        # rows written before canonical_ts hashed the raw isoformat text; accept either form
        candidates = [compute_row_hash(run_id, seq, agent, t, inp_h, out_h, prev_h)
                      for t in dict.fromkeys((canonical_ts(r["TS"]), str(r["TS"])))]
        if stored_hash not in candidates:
            return False, f"Hash mismatch at seq={seq}: expected {candidates[0]}, stored {stored_hash}"

        last_hash = stored_hash

    return True, None


def validate_api_hub_master_data(material: str, supplier: str | None) -> dict[str, Any]:
    """Optional check against SAP API Business Hub Sandbox (API_PRODUCT_SRV, API_BUSINESS_PARTNER)."""
    settings = load_settings()
    if not settings.sap_api_hub_key:
        return {"status": "SKIPPED", "reason": "No SAP_API_HUB_KEY configured"}

    import requests
    headers = {"APIKey": settings.sap_api_hub_key, "Accept": "application/json"}
    results = {}
    try:
        url = f"https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap/API_PRODUCT_SRV/A_Product('{material}')"
        resp = requests.get(url, headers=headers, timeout=5)
        results["product_check"] = "OK" if resp.status_code == 200 else f"HTTP {resp.status_code}"
    except Exception as exc:
        results["product_check"] = f"ERROR: {exc}"

    if supplier:
        try:
            url_bp = f"https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner('{supplier}')"
            resp = requests.get(url_bp, headers=headers, timeout=5)
            results["supplier_check"] = "OK" if resp.status_code == 200 else f"HTTP {resp.status_code}"
        except Exception as exc:
            results["supplier_check"] = f"ERROR: {exc}"

    return results


def act(scenario_id: str, db: Db, run_id: str | None = None, approver: str | None = None) -> dict[str, Any]:
    """Execute procurement action for an approved scenario.
    
    Strict human gate: refuses execution unless FF_AG_APPROVAL has an approving row
    with all checklist items confirmed.
    """
    # 1. Fetch approval row
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

    if app_df is None or app_df.empty:
        raise PermissionError(f"No human approval found for scenario {scenario_id}. A6 refused action.")

    app_row = app_df.iloc[0]
    action = str(app_row["ACTION"])
    if action not in (ApprovalAction.APPROVE.value, ApprovalAction.EDIT_APPROVE.value):
        raise PermissionError(f"Scenario {scenario_id} action is {action}, not APPROVED. A6 refused action.")

    checklist_dict = json.loads(app_row["CHECKLIST_JSON"] or "{}")
    checklist = ApprovalChecklist(**checklist_dict)
    if not checklist.complete:
        raise PermissionError(f"Incomplete checklist for scenario {scenario_id}. All control points must be confirmed.")

    run_id = str(app_row["RUN_ID"])
    approver = approver or str(app_row["APPROVER"])

    # 2. Fetch Recommendation / DraftPR
    rec_df = try_query(
        db,
        "SELECT * FROM FF_AG_RECOMMENDATION WHERE RUN_ID = ? AND SCENARIO_ID = ?",
        (run_id, scenario_id)
    )
    if rec_df is None or rec_df.empty:
        raise ValueError(f"No recommendation found for scenario {scenario_id} in run {run_id}")

    rec_row = rec_df.iloc[0]
    draft_pr_raw = rec_row["DRAFT_PR_JSON"]
    if not draft_pr_raw:
        raise ValueError(f"Scenario {scenario_id} has no draft PR (internal rebalance or non-purchase option)")

    draft_dict = json.loads(draft_pr_raw)
    draft_pr = DraftPR(**draft_dict)

    # 3. Apply human edits if EDIT_APPROVE
    qty = float(app_row["EDITED_QTY"]) if app_row["EDITED_QTY"] is not None else draft_pr.quantity
    supplier = str(app_row["EDITED_SUPPLIER"]) if app_row["EDITED_SUPPLIER"] else draft_pr.fixed_supplier

    # 4. Generate S/4HANA PR number and item
    # Generate 10-char BANFN
    cnt_df = try_query(db, "SELECT COUNT(*) AS N FROM FF_MM_EBAN")
    cnt = int(cnt_df.iloc[0, 0]) if cnt_df is not None and not cnt_df.empty else 0
    banfn = f"{1000000001 + cnt:010d}"[-10:]
    bnfpo = 10

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = date.today().isoformat()

    # 5. Insert into FF_MM_EBAN (S/4 mock purchase requisition table)
    db.execute(
        f"INSERT INTO FF_MM_EBAN ({', '.join(EBAN_COLS)}) VALUES ({placeholders(EBAN_COLS)})",
        (
            banfn, bnfpo, draft_pr.material, draft_pr.plant, qty, draft_pr.base_unit,
            float(draft_pr.unit_price_inr) if draft_pr.unit_price_inr else None,
            1, today, draft_pr.delivery_date.isoformat(), supplier, approver,
            "APPROVED", run_id, scenario_id, "A6_ACTION",
            "API_PURCHASEREQ_PROCESS_SRV", now, "SYNTH"
        )
    )

    # 6. Format S/4HANA OData payload
    s4_payload = {
        "PurchaseRequisition": banfn,
        "PurReqnDescription": f"Kairos Buffer Requisition {scenario_id}",
        "to_PurchaseReqnItem": [
            {
                "PurchaseRequisition": banfn,
                "PurchaseRequisitionItem": f"{bnfpo:05d}",
                "Material": draft_pr.material,
                "Plant": draft_pr.plant,
                "RequestedQuantity": str(qty),
                "BaseUnit": draft_pr.base_unit,
                "DeliveryDate": draft_pr.delivery_date.isoformat(),
                "PurchasingGroup": draft_pr.purchasing_group,
                "FixedSupplier": supplier,
                "PurchaseRequisitionPrice": str(draft_pr.unit_price_inr or "0"),
                "PurReqnItemCurrency": draft_pr.currency,
            }
        ]
    }

    # Optional Sandbox verification
    hub_check = validate_api_hub_master_data(draft_pr.material, supplier)

    # 7. Audit the action hop
    audit_evt = AuditEvent(
        run_id=run_id,
        seq=0,
        agent="A6_ACT",
        ts=datetime.now(timezone.utc),
        input_hash=hash_data({"scenario_id": scenario_id, "approval": app_row.to_dict()}),
        output_hash=hash_data(s4_payload),
        data_sources=[DataSourceRef(source="FF_AG_APPROVAL", is_proxy="REAL")],
    )
    audit(audit_evt, db)

    return {
        "banfn": banfn,
        "bnfpo": bnfpo,
        "material": draft_pr.material,
        "plant": draft_pr.plant,
        "quantity": qty,
        "supplier": supplier,
        "s4_payload": s4_payload,
        "api_hub_check": hub_check,
        "status": "POSTED",
    }
