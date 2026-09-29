"""Small helpers shared by agents (A2 onward): ctx resolution, optional-table queries, tag handling.

A1 still carries its own copies; fold them in here in a later cleanup.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence

import pandas as pd

from agents.contracts import DataTag, RunContext
from agents.ctx import Db, get_db
from agents.rules import Rules, load_rules

_TAG_ORDER = {DataTag.REAL: 0, DataTag.PROXY: 1, DataTag.SYNTH: 2}
_MISSING_TABLE = ("no such table", "invalid table name", "259")


@dataclass
class AgentCtx:
    db: Db
    rules: Rules
    run: RunContext


def resolve_ctx(ctx: Any, prefix: str) -> AgentCtx:
    """Accept a Db, or any object with .db and optional .rules / .run (RunContext)."""
    if ctx is None:
        db = get_db()
    else:
        db = ctx if isinstance(ctx, Db) else (getattr(ctx, "db", None) or get_db())
    rules = getattr(ctx, "rules", None) or load_rules()
    run = getattr(ctx, "run", None) or RunContext(run_id=f"{prefix}-{uuid.uuid4().hex[:12]}")
    return AgentCtx(db, rules, run)


def try_query(db: Db, sql: str, params: Sequence[Any] = ()) -> pd.DataFrame | None:
    """Run a query; return None if the table/view doesn't exist (optional inputs)."""
    try:
        return db.query(sql, params)
    except Exception as exc:  # sqlite3.OperationalError / hdbcli ProgrammingError
        if any(m in str(exc).lower() for m in _MISSING_TABLE):
            return None
        raise


def to_tag(raw: Any) -> DataTag:
    """Normalise an IS_PROXY / tag cell. Unknown or empty counts as SYNTH (least trusted)."""
    s = str(raw).strip().upper() if raw is not None else ""
    if s in ("REAL", "N", "0", "FALSE"):
        return DataTag.REAL
    if s in ("PROXY", "Y", "1", "TRUE"):
        return DataTag.PROXY
    return DataTag.SYNTH


def worst_tag(values: Sequence[Any]) -> DataTag:
    tags = [to_tag(v) for v in values]
    return max(tags, key=_TAG_ORDER.__getitem__) if tags else DataTag.SYNTH


def placeholders(ids: Sequence[Any]) -> str:
    return ", ".join("?" for _ in ids)


def db_ts(dt: datetime | None = None) -> str:
    """UTC timestamp text both HANA TIMESTAMP and SQLite accept."""
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def upsert(db: Db, table: str, pk: Sequence[str], row: dict[str, Any]) -> None:
    """Insert or replace one row by primary key on HANA (UPSERT) or SQLite (ON CONFLICT)."""
    upsert_many(db, table, pk, [row])


def upsert_many(db: Db, table: str, pk: Sequence[str], rows: Sequence[dict[str, Any]]) -> int:
    """Insert or replace rows (same columns) by primary key in one batched round trip."""
    if not rows:
        return 0
    cols = list(rows[0])
    names, marks = ", ".join(cols), placeholders(cols)
    if db.backend == "hana":
        sql = f"UPSERT {table} ({names}) VALUES ({marks}) WITH PRIMARY KEY"
    else:
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in pk)
        sql = (f"INSERT INTO {table} ({names}) VALUES ({marks}) ON CONFLICT ({', '.join(pk)}) "
               + (f"DO UPDATE SET {updates}" if updates else "DO NOTHING"))
    return db.executemany(sql, [[r[c] for c in cols] for r in rows])


REPLICA_TABLES = (
    "FF_CFG_PARAM", "FF_REF_FORMULATION", "FF_REF_CEILING_PRICE", "FF_REF_API", "FF_REF_FORM_API",
    "FF_REF_BOM_ASSUMPTION", "FF_REF_API_COST_MONTHLY", "FF_REF_API_ORIGIN", "FF_REF_MANUFACTURER",
    "FF_REF_PRODUCER", "FF_REF_WPI", "FF_REF_NSQ_ALERT", "FF_MM_MARA", "FF_MM_LFA1", "FF_MM_T001W", "FF_MM_MCHB",
    "FF_MM_MSEG", "FF_MM_EKKO", "FF_MM_EKPO", "FF_MM_COLDCHAIN", "FF_G_V", "FF_G_E",
)
REPLICA_MAX_AGE_S = 900
_replica: dict[str, tuple[float, Db]] = {}


def _cell(v: Any) -> Any:
    """HANA value -> something sqlite3 binds (dates as ISO text, Decimal as float, numpy scalars unwrapped)."""
    if v is None or v is pd.NaT or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    if hasattr(v, "is_finite"):
        return float(v)
    return v.item() if hasattr(v, "item") else v


def read_replica(db: Db, settings: Any) -> Db:
    """In-memory SQLite copy of the reference and MM tables, so read-heavy agents (A1/A4/A5, planning) don't pay
    a HANA round trip per lookup. Built with parallel bulk reads, cached for REPLICA_MAX_AGE_S. SQLite is returned
    as-is. Only reads may use the replica: every write still goes to `db`."""
    import sqlite3
    import time
    from concurrent.futures import ThreadPoolExecutor

    from ingest import load_hana as lh

    if db.backend != "hana":
        return db
    key = f"{settings.hana_host}/{settings.hana_schema}"
    hit = _replica.get(key)
    if hit and time.time() - hit[0] < REPLICA_MAX_AGE_S:
        return hit[1]

    def fetch(tables: Sequence[str]) -> dict[str, pd.DataFrame | None]:
        with get_db(settings) as conn:
            return {t: try_query(conn, f"SELECT * FROM {t}") for t in tables}

    chunks = [REPLICA_TABLES[i::4] for i in range(4)]
    frames: dict[str, pd.DataFrame | None] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for part in ex.map(fetch, chunks):
            frames.update(part)
    mem = Db(sqlite3.connect(":memory:", check_same_thread=False), "sqlite")
    lh.create_objects(mem)
    schema = lh.schema_tables()
    for t, df in frames.items():
        if df is None or df.empty:
            continue
        cols = [c for c in df.columns if c in schema[t].columns]
        mem.executemany(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({placeholders(cols)})",
                        [[_cell(v) for v in r] for r in df[cols].itertuples(index=False, name=None)])
    _replica[key] = (time.time(), mem)
    return mem


def po_lines(db: Db, open_only: bool = False) -> pd.DataFrame:
    """PO lines with vendor and per-unit net price (UNIT_NET). open_only: no goods receipt (101) posted yet."""
    sql = ("SELECT p.EBELN, p.EBELP, p.MATNR, p.WERKS, p.MENGE, p.NETPR, p.PEINH, p.EINDT, h.LIFNR, h.BEDAT "
           "FROM FF_MM_EKPO p JOIN FF_MM_EKKO h ON h.EBELN = p.EBELN")
    if open_only:
        sql += (" WHERE NOT EXISTS (SELECT 1 FROM FF_MM_MSEG s WHERE s.EBELN = p.EBELN AND s.EBELP = p.EBELP "
                "AND s.BWART = '101')")
    df = try_query(db, sql + " ORDER BY p.EINDT, p.EBELN, p.EBELP")
    cols = ["EBELN", "EBELP", "MATNR", "WERKS", "MENGE", "NETPR", "PEINH", "EINDT", "LIFNR", "BEDAT", "UNIT_NET"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    per = pd.to_numeric(df["PEINH"], errors="coerce").fillna(1.0).clip(lower=1.0)
    df["UNIT_NET"] = pd.to_numeric(df["NETPR"], errors="coerce") / per
    df["MENGE"] = pd.to_numeric(df["MENGE"], errors="coerce").fillna(0.0)
    df["EINDT"] = pd.to_datetime(df["EINDT"].astype(str)).dt.date
    df["BEDAT"] = pd.to_datetime(df["BEDAT"].astype(str)).dt.date
    return df


def lob_cols(db: Db, col: str) -> str:
    """Select an NCLOB as two columns <col>_S / <col>_L. On HANA a LOB costs a round trip per row, so values up to
    5000 chars come back as NVARCHAR in _S and only longer ones as a LOB in _L. Read with lob_value()."""
    if db.backend != "hana":
        return f"{col} AS {col}_S, NULL AS {col}_L"
    return (f"CASE WHEN LENGTH({col}) <= 5000 THEN TO_NVARCHAR({col}) END AS {col}_S, "
            f"CASE WHEN LENGTH({col}) > 5000 THEN {col} END AS {col}_L")


def lob_value(row: Any, col: str) -> str | None:
    s = row[f"{col}_S"]
    s = s if s is not None and s == s else row[f"{col}_L"]
    return None if s is None or s != s else str(s)


def as_of_date(db: Db) -> date:
    """The data as-of date (FF_CFG_PARAM AS_OF_DATE via FF_V_ASOF), else today."""
    df = try_query(db, "SELECT AS_OF FROM FF_V_ASOF")
    if df is not None and not df.empty and df.iat[0, 0] is not None:
        return pd.to_datetime(str(df.iat[0, 0])).date()
    return datetime.now(timezone.utc).date()


def load_fixture_db(as_of: str | None = None) -> Db:
    """In-memory SQLite with the S01 schema + views, seeded from the SYNTH test fixtures (tests and --fixtures CLIs).

    The fixture CSVs carry no ceiling effective date (the S01 adapter uses FETCHED_AT), so it is set before the
    first cost month; otherwise every earlier month would correctly have no ceiling and A1 could not trend.
    """
    from agents.ctx import Settings, get_db
    from ingest import load_hana as lh
    from tests.conftest import FIXTURES

    db = get_db(Settings(db_backend="sqlite"), sqlite_path=":memory:")
    lh.create_objects(db)
    lh.seed(db, [FIXTURES])
    db.execute("UPDATE FF_REF_CEILING_PRICE SET EFFECTIVE_FROM = ? WHERE SOURCE = ?", (FIXTURE_CEILING_FROM, "FIXTURE_S00"))
    if as_of:
        db.execute("INSERT INTO FF_CFG_PARAM (NAME, DATE_VALUE) VALUES ('AS_OF_DATE', ?)", (as_of,))
    return db


FIXTURE_CEILING_FROM = "2025-04-01"


def table_columns(db: Db, table: str) -> set[str] | None:
    """Upper-case column names of a table/view, or None if it doesn't exist."""
    df = try_query(db, f"SELECT * FROM {table} WHERE 1 = 0")
    return None if df is None else {str(c).upper() for c in df.columns}
