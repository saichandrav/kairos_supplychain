"""Create the FF_ schema and views, and load seed CSVs, on HANA Cloud or SQLite (plan §6, §7).

    python -m ingest.load_hana --backend sqlite --reset --seed
    python -m ingest.load_hana --backend hana --reset --seed
    python -m ingest.load_hana --gen-sqlite      # regenerate db/*_sqlite.sql from db/*.sql

db/schema.sql and db/views.sql (HANA dialect) are the single source of truth; the SQLite files are
generated from them by `to_sqlite_*` below. Everything is idempotent:
  * no flag      create missing tables, recreate the views;
  * --reset      drop every FF_ object in OUR OWN schema first (HANA: refuses unless the current
                 schema is owned by the connected user), then create;
  * --seed       upsert CSVs from data/seed/ and tests/fixtures/ (whichever exist).
Seed CSVs named after a table (FF_REF_API.csv) load 1:1. The S00 fixture files use their own
shapes and are mapped onto the §6 tables by FIXTURE_ADAPTERS.
Never prints the HANA host, user or password (see `scrub`).
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from agents.contracts import DataTag
from agents.ctx import REPO_ROOT, SQLITE_PATH, Db, get_db, load_settings

DB_DIR = REPO_ROOT / "db"
SQL_FILES = {
    "hana": (DB_DIR / "schema.sql", DB_DIR / "views.sql"),
    "sqlite": (DB_DIR / "schema_sqlite.sql", DB_DIR / "views_sqlite.sql"),
}
SEED_DIRS = (REPO_ROOT / "data" / "seed", REPO_ROOT / "tests" / "fixtures")
PROVENANCE = ("SOURCE", "SOURCE_URL", "FETCHED_AT", "IS_PROXY")
DATA_TAGS = frozenset(t.value for t in DataTag)
APPEND_ONLY = frozenset({"FF_AG_AUDIT_LOG"})
GENERATED_HEADER = "-- GENERATED from db/{src} by `python -m ingest.load_hana --gen-sqlite`. Do not edit.\n\n"
_FF_NAME = re.compile(r"^FF_[A-Z0-9_]+$")


# ---------------------------------------------------------------- SQL files


@dataclass(frozen=True)
class Table:
    name: str
    columns: dict[str, str]  # column -> HANA type, e.g. "DECIMAL(15,4)"
    pk: tuple[str, ...]

    @property
    def has_provenance(self) -> bool:
        return all(c in self.columns for c in PROVENANCE)


def split_statements(sql: str) -> list[str]:
    """Strip `--` comments and split on `;`. Our SQL files never put `--` or `;` inside strings."""
    body = "\n".join(line.split("--", 1)[0].rstrip() for line in sql.splitlines())
    return [s.strip() for s in body.split(";") if s.strip()]


_CREATE_TABLE = re.compile(r"CREATE\s+COLUMN\s+TABLE\s+(\w+)\s*\((.*)\)\s*$", re.S)
_COLUMN = re.compile(r"^(\w+)\s+([A-Z]+(?:\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)")
_PK = re.compile(r"^PRIMARY\s+KEY\s*\(([^)]*)\)")


def parse_tables(schema_sql: str) -> dict[str, Table]:
    """Tables, column types and primary keys from db/schema.sql (one column per line)."""
    tables: dict[str, Table] = {}
    for stmt in split_statements(schema_sql):
        m = _CREATE_TABLE.match(stmt)
        if not m:
            continue
        cols: dict[str, str] = {}
        pk: tuple[str, ...] = ()
        for line in (ln.strip().rstrip(",") for ln in m.group(2).splitlines()):
            if pk_m := _PK.match(line):
                pk = tuple(c.strip() for c in pk_m.group(1).split(","))
            elif col_m := _COLUMN.match(line):
                cols[col_m.group(1)] = re.sub(r"\s+", "", col_m.group(2))
        if not pk:
            raise ValueError(f"{m.group(1)} has no PRIMARY KEY line")
        tables[m.group(1)] = Table(m.group(1), cols, pk)
    return tables


def schema_tables() -> dict[str, Table]:
    return parse_tables(SQL_FILES["hana"][0].read_text(encoding="utf-8"))


_SQLITE_TYPES = (
    (re.compile(r"\bCREATE\s+COLUMN\s+TABLE\b"), "CREATE TABLE"),
    (re.compile(r"\bN?VARCHAR\(\d+\)"), "TEXT"),
    (re.compile(r"\bN?CLOB\b"), "TEXT"),
    (re.compile(r"\bDECIMAL\(\d+,\s*\d+\)"), "NUMERIC"),
    (re.compile(r"\b(?:BIGINT|INTEGER|SMALLINT|TINYINT)\b"), "INTEGER"),
    (re.compile(r"\bDOUBLE\b"), "REAL"),
    (re.compile(r"\b(?:TIMESTAMP|DATE)\b(?!\s*\()"), "TEXT"),
)
_ARG = r"([A-Za-z_][\w.]*)"
_SQLITE_FUNCS = (
    (re.compile(rf"\bADD_DAYS\(\s*{_ARG}\s*,\s*(-?\d+)\s*\)"), r"DATE(\1, '\2 days')"),
    (re.compile(rf"\bDAYS_BETWEEN\(\s*{_ARG}\s*,\s*{_ARG}\s*\)"), r"CAST(JULIANDAY(\2) - JULIANDAY(\1) AS INTEGER)"),
    (re.compile(r"\bLEAST\("), "MIN("),
    (re.compile(r"\bGREATEST\("), "MAX("),
)


def _to_sqlite(sql: str, rules: tuple) -> list[str]:
    out = []
    for stmt in split_statements(sql):
        for pattern, repl in rules:
            stmt = pattern.sub(repl, stmt)
        if leftover := re.search(r"\b(ADD_DAYS|DAYS_BETWEEN|COLUMN\s+TABLE)\b", stmt):
            raise ValueError(f"not portable to SQLite (use plain column/int args): {leftover.group(0)}")
        out.append(stmt)
    return out


def to_sqlite_schema(sql: str) -> list[str]:
    return _to_sqlite(sql, _SQLITE_TYPES)


def to_sqlite_views(sql: str) -> list[str]:
    return _to_sqlite(sql, _SQLITE_FUNCS)


def render_sqlite_files() -> dict[Path, str]:
    """Generated content of db/schema_sqlite.sql and db/views_sqlite.sql."""
    (schema_src, views_src), (schema_dst, views_dst) = SQL_FILES["hana"], SQL_FILES["sqlite"]
    out = {}
    for src, dst, fn in ((schema_src, schema_dst, to_sqlite_schema), (views_src, views_dst, to_sqlite_views)):
        stmts = fn(src.read_text(encoding="utf-8"))
        out[dst] = GENERATED_HEADER.format(src=src.name) + "".join(f"{s};\n\n" for s in stmts).rstrip() + "\n"
    return out


def gen_sqlite() -> list[Path]:
    written = []
    for path, text in render_sqlite_files().items():
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


# ---------------------------------------------------------------- DDL


def _check_name(name: str) -> str:
    if not _FF_NAME.match(name):
        raise ValueError(f"refusing to touch non-FF_ object {name!r}")
    return name


def ff_objects(db: Db) -> dict[str, str]:
    """FF_ tables and views in our own schema -> 'table' | 'view'."""
    if db.backend == "sqlite":
        df = db.query("SELECT name, type FROM sqlite_master "
                      "WHERE type IN ('table', 'view') AND name LIKE 'FF\\_%' ESCAPE '\\'")
    else:
        df = db.query(
            "SELECT TABLE_NAME, 'table' FROM SYS.TABLES WHERE SCHEMA_NAME = CURRENT_SCHEMA "
            "AND TABLE_NAME LIKE 'FF\\_%' ESCAPE '\\' "
            "UNION ALL SELECT VIEW_NAME, 'view' FROM SYS.VIEWS WHERE SCHEMA_NAME = CURRENT_SCHEMA "
            "AND VIEW_NAME LIKE 'FF\\_%' ESCAPE '\\'")
    return {str(name): str(kind) for name, kind in df.itertuples(index=False)}


def assert_own_schema(db: Db) -> None:
    """HANA: only operate when the current schema belongs to the connected user."""
    if db.backend != "hana":
        return
    df = db.query("SELECT COUNT(*) FROM SYS.SCHEMAS "
                  "WHERE SCHEMA_NAME = CURRENT_SCHEMA AND SCHEMA_OWNER = CURRENT_USER")
    if int(df.iat[0, 0]) != 1:
        raise RuntimeError("current schema is not owned by the connected user; refusing to change it "
                           "(set HANA_SCHEMA to your own schema)")


def drop_ff_objects(db: Db) -> list[str]:
    """Drop every FF_ graph workspace, view and table in our own schema. Nothing else."""
    assert_own_schema(db)
    dropped = []
    if db.backend == "hana":
        spaces = db.query("SELECT WORKSPACE_NAME FROM SYS.GRAPH_WORKSPACES WHERE SCHEMA_NAME = CURRENT_SCHEMA "
                          "AND WORKSPACE_NAME LIKE 'FF\\_%' ESCAPE '\\'")
        for (ws,) in spaces.itertuples(index=False):
            db.execute(f'DROP GRAPH WORKSPACE "{_check_name(ws)}"')
            dropped.append(ws)
    objects = ff_objects(db)
    for kind in ("view", "table"):
        for name in sorted(n for n, k in objects.items() if k == kind):
            db.execute(f'DROP {kind.upper()} "{_check_name(name)}"')
            dropped.append(name)
    return dropped


_CREATE_NAME = re.compile(r"CREATE\s+(?:COLUMN\s+)?(TABLE|VIEW)\s+(\w+)", re.I)


def create_objects(db: Db) -> tuple[int, int]:
    """Create missing tables and (re)create all views. Returns (tables created, views created)."""
    assert_own_schema(db)
    schema_path, views_path = SQL_FILES[db.backend]
    existing = ff_objects(db)
    n_tables = 0
    for stmt in split_statements(schema_path.read_text(encoding="utf-8")):
        name = _check_name(_CREATE_NAME.match(stmt).group(2))
        if name not in existing:
            db.execute(stmt)
            n_tables += 1
    view_stmts = split_statements(views_path.read_text(encoding="utf-8"))
    for stmt in reversed(view_stmts):
        name = _check_name(_CREATE_NAME.match(stmt).group(2))
        if existing.get(name) == "view":
            db.execute(f'DROP VIEW "{name}"')
    widen_columns(db)
    for stmt in view_stmts:
        db.execute(stmt)
    return n_tables, len(view_stmts)


def widen_columns(db: Db) -> list[str]:
    """HANA: grow existing (N)VARCHAR columns to their db/schema.sql length. Never shrinks; SQLite needs nothing."""
    if db.backend != "hana":
        return []
    live = db.query("SELECT TABLE_NAME, COLUMN_NAME, LENGTH FROM SYS.TABLE_COLUMNS WHERE SCHEMA_NAME = CURRENT_SCHEMA "
                    "AND TABLE_NAME LIKE 'FF\\_%' ESCAPE '\\'")
    have = {(str(t), str(c)): int(n) for t, c, n in live.itertuples(index=False)}
    widened = []
    for table in schema_tables().values():
        for col, typ in table.columns.items():
            m = re.fullmatch(r"N?VARCHAR\((\d+)\)", typ)
            cur = have.get((table.name, col))
            if m and cur is not None and cur < int(m.group(1)):
                db.execute(f'ALTER TABLE "{_check_name(table.name)}" ALTER ("{col}" {typ})')
                widened.append(f"{table.name}.{col}")
    return widened


# ---------------------------------------------------------------- seed CSVs

Rows = list[dict[str, Any]]


def _read_csv(path: Path) -> Rows:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [{k.strip().upper(): (v.strip() if v is not None else "") for k, v in r.items()}
                for r in csv.DictReader(fh)]


def _prov(row: dict[str, Any]) -> dict[str, Any]:
    return {c: row.get(c, "") for c in PROVENANCE}


def _month_start(value: str) -> str:
    """'2025-10' or an ISO date/timestamp -> '2025-10-01'."""
    return value[:7] + "-01"


# Fixture files (S00, tests/fixtures/) -> §6 tables. Where a fixture lacks a column that is part of
# a key (ceiling EFFECTIVE_FROM, origin MONTH) the row's FETCHED_AT date stands in: "known as of".
def _fx_formulations(rows: Rows, files: dict[str, Rows]) -> dict[str, Rows]:
    bom = {r["FORMULATION_ID"]: r for r in files.get("bom_assumption", [])}
    out: dict[str, Rows] = defaultdict(list)
    for r in rows:
        fid, p = r["FORMULATION_ID"], _prov(r)
        out["FF_REF_FORMULATION"].append({"FORM_ID": fid, "GENERIC": r["NAME"], "STRENGTH": r["STRENGTH"],
                                          "DOSAGE_FORM": r["DOSAGE_FORM"], "UNIT": r["UNIT"],
                                          "NLEM_LEVEL": r["NLEM_LEVEL"], **p})
        out["FF_REF_CEILING_PRICE"].append({"FORM_ID": fid, "EFFECTIVE_FROM": r["FETCHED_AT"][:10],
                                            "SO_NUMBER": r["CEILING_SO_NO"],
                                            "CEILING_PRICE": r["CEILING_PRICE_INR"], **p})
        g = bom.get(fid, {}).get("API_G_PER_UNIT", "")
        mg = str(Decimal(g) * 1000) if g else ""
        out["FF_REF_FORM_API"].append({"FORM_ID": fid, "API_ID": r["API_ID"], "API_MG_PER_UNIT": mg, **p})
    return out


def _fx_bom(rows: Rows, _files: dict[str, Rows]) -> dict[str, Rows]:
    cols = ("API_G_PER_UNIT", "YIELD", "CONVERSION_COST_INR", "PACKAGING_COST_INR", "FREIGHT_COST_INR")
    return {"FF_REF_BOM_ASSUMPTION": [{"FORM_ID": r["FORMULATION_ID"], **{c: r[c] for c in cols}, **_prov(r)}
                                      for r in rows]}


def _fx_apis(rows: Rows, _files: dict[str, Rows]) -> dict[str, Rows]:
    return {
        "FF_REF_API": [{"API_ID": r["API_ID"], "NAME": r["NAME"], "HS8": r["HS_CODE"], **_prov(r)} for r in rows],
        "FF_REF_API_ORIGIN": [{"API_ID": r["API_ID"], "MONTH": _month_start(r["FETCHED_AT"]),
                               "COUNTRY": r["TOP_ORIGIN_COUNTRY"], "SHARE": r["TOP_ORIGIN_SHARE"], **_prov(r)}
                              for r in rows if r["TOP_ORIGIN_COUNTRY"]],
    }


def _fx_api_cost(rows: Rows, _files: dict[str, Rows]) -> dict[str, Rows]:
    return {"FF_REF_API_COST_MONTHLY": [{"API_ID": r["API_ID"], "MONTH": _month_start(r["MONTH"]),
                                        "UNIT_VALUE_INR_KG": r["COST_PER_KG_INR"], **_prov(r)} for r in rows]}


def _fx_producers(rows: Rows, _files: dict[str, Rows]) -> dict[str, Rows]:
    makers = {r["MANUFACTURER_ID"]: {"ID": r["MANUFACTURER_ID"], "NAME": r["MANUFACTURER_NAME"], **_prov(r)}
              for r in rows}
    return {
        "FF_REF_PRODUCER": [{"FORM_ID": r["FORMULATION_ID"], "MANUFACTURER_ID": r["MANUFACTURER_ID"],
                             "EVIDENCE_SOURCE": r["SOURCE"], "MARKET_SHARE": r["MARKET_SHARE"], **_prov(r)}
                            for r in rows],
        "FF_REF_MANUFACTURER": list(makers.values()),
    }


def _fx_mara(rows: Rows, _files: dict[str, Rows]) -> dict[str, Rows]:
    return {"FF_MM_MARA": [{"MATNR": r["MATNR"], "MAKTX": r["MAKTX"], "FORM_ID": r["FORMULATION_ID"],
                            "MEINS": r["MEINS"], "VED": r["VED"],
                            "RAUBE": "COLD" if r["COLD_CHAIN"] == "Y" else "AMBIENT", **_prov(r)} for r in rows]}


def _fx_mchb(rows: Rows, _files: dict[str, Rows]) -> dict[str, Rows]:
    """QUARANTINE=Y stock is quality-inspection stock (CINSM), not unrestricted (CLABS)."""
    out = []
    for r in rows:
        q = r["QUARANTINE"] == "Y"
        out.append({"MATNR": r["MATNR"], "WERKS": r["WERKS"], "LGORT": r["LGORT"], "CHARG": r["CHARG"],
                    "CLABS": "0" if q else r["CLABS"], "CINSM": r["CLABS"] if q else "0",
                    "VFDAT": r["VFDAT"], **_prov(r)})
    return {"FF_MM_MCHB": out}


def _fx_mseg(rows: Rows, _files: dict[str, Rows]) -> dict[str, Rows]:
    return {"FF_MM_MSEG": [{"MBLNR": r["MBLNR"], "MJAHR": r["BUDAT"][:4], "ZEILE": "1", "BWART": r["BWART"],
                            "MATNR": r["MATNR"], "WERKS": r["WERKS"], "LGORT": r["LGORT"], "MENGE": r["MENGE"],
                            "BUDAT": r["BUDAT"], "KOSTL": r["KOSTL"], **_prov(r)} for r in rows]}


FIXTURE_ADAPTERS: dict[str, Callable[[Rows, dict[str, Rows]], dict[str, Rows]]] = {
    "formulations": _fx_formulations,
    "bom_assumption": _fx_bom,
    "apis": _fx_apis,
    "api_cost_monthly": _fx_api_cost,
    "producers": _fx_producers,
    "mara": _fx_mara,
    "mchb": _fx_mchb,
    "mseg": _fx_mseg,
}


def collect_seed_rows(dirs: tuple[Path, ...] | list[Path], tables: dict[str, Table]) -> tuple[dict[str, Rows], list[str]]:
    """CSV rows per target table from every existing dir, plus names of files that were skipped."""
    out: dict[str, Rows] = defaultdict(list)
    skipped: list[str] = []
    for d in dirs:
        if not d.is_dir():
            continue
        files = {p.stem: _read_csv(p) for p in sorted(d.glob("*.csv"))}
        for stem, rows in files.items():
            if stem.upper() in tables:
                out[stem.upper()].extend(rows)
            elif stem in FIXTURE_ADAPTERS:
                for table, trows in FIXTURE_ADAPTERS[stem](rows, files).items():
                    out[table].extend(trows)
            else:
                skipped.append(f"{d.name}/{stem}.csv")
    return out, skipped


def _convert(value: Any, typ: str, table: str, col: str) -> Any:
    if value is None or value == "":
        return None
    v = str(value)
    if (n := re.fullmatch(r"N?VARCHAR\((\d+)\)", typ)) and len(v) > int(n.group(1)):
        raise ValueError(f"{table}.{col}: {len(v)} characters > {typ}: {v[:40]!r}")  # HANA enforces this; SQLite does not
    try:
        if typ in ("INTEGER", "BIGINT", "SMALLINT", "TINYINT"):
            return int(v)
        if typ.startswith("DECIMAL"):
            return Decimal(v)
        if typ == "DOUBLE":
            return float(v)
        if typ == "DATE":
            return date.fromisoformat(v[:10])
        if typ == "TIMESTAMP":
            ts = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts
    except ValueError as exc:
        raise ValueError(f"{table}.{col}: bad {typ} value {v!r}") from exc
    return v


def _for_sqlite(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, Decimal)):
        return str(value)
    return value


def _upsert_sql(db: Db, table: Table, cols: list[str]) -> str:
    names = ", ".join(f'"{c}"' for c in cols)
    marks = ", ".join("?" for _ in cols)
    if db.backend == "hana":
        return f'UPSERT "{table.name}" ({names}) VALUES ({marks}) WITH PRIMARY KEY'
    updates = ", ".join(f'"{c}" = excluded."{c}"' for c in cols if c not in table.pk)
    action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    pk = ", ".join(f'"{c}"' for c in table.pk)
    return f'INSERT INTO "{table.name}" ({names}) VALUES ({marks}) ON CONFLICT ({pk}) {action}'


def upsert_rows(db: Db, table: Table, rows: Rows, chunk: int = 500) -> int:
    """Validate, type-convert and upsert rows by primary key. Idempotent."""
    if table.name in APPEND_ONLY:
        raise ValueError(f"{table.name} is append-only (A6 writes it); it cannot be seeded")
    groups: dict[tuple[str, ...], list[tuple]] = defaultdict(list)
    for i, row in enumerate(rows, 1):
        unknown = set(row) - set(table.columns)
        if unknown:
            raise ValueError(f"{table.name} row {i}: unknown columns {sorted(unknown)}")
        if table.has_provenance:
            if not row.get("SOURCE"):
                raise ValueError(f"{table.name} row {i}: SOURCE is required")
            if row.get("IS_PROXY") not in DATA_TAGS:
                raise ValueError(f"{table.name} row {i}: IS_PROXY must be one of {sorted(DATA_TAGS)}, "
                                 f"got {row.get('IS_PROXY')!r}")
        cols = tuple(row)
        values = tuple(_convert(row[c], table.columns[c], table.name, c) for c in cols)
        if db.backend == "sqlite":
            values = tuple(_for_sqlite(v) for v in values)
        groups[cols].append(values)
    n = 0
    for cols, values in groups.items():
        sql = _upsert_sql(db, table, list(cols))
        for start in range(0, len(values), chunk):
            n += db.executemany(sql, values[start:start + chunk])
    return n


def seed(db: Db, dirs: tuple[Path, ...] | list[Path] = SEED_DIRS) -> tuple[dict[str, int], list[str]]:
    """Upsert all seed CSVs. Returns (rows per table, skipped files)."""
    tables = schema_tables()
    rows, skipped = collect_seed_rows(dirs, tables)
    return {t: upsert_rows(db, tables[t], r) for t, r in sorted(rows.items())}, skipped


# ---------------------------------------------------------------- CLI


def scrub(text: object) -> str:
    """Remove HANA host, user and password values (and any *.hanacloud host) from a message."""
    s = str(text)
    for key in ("HANA_PASSWORD", "HANA_HOST", "HANA_USER", "HANA_SCHEMA"):
        val = os.environ.get(key, "")
        if len(val) >= 3:
            s = re.sub(re.escape(val), "***", s, flags=re.I)
    return re.sub(r"[\w.-]+\.hanacloud\.ondemand\.com", "***", s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ingest.load_hana", description=__doc__.split("\n\n")[0])
    ap.add_argument("--backend", choices=("hana", "sqlite"), help="default: DB_BACKEND from env")
    ap.add_argument("--reset", action="store_true", help="drop and recreate every FF_ object in our schema")
    ap.add_argument("--seed", action="store_true", help="upsert CSVs from data/seed/ and tests/fixtures/")
    ap.add_argument("--sqlite-path", default=str(SQLITE_PATH), help="SQLite file (default: %(default)s)")
    ap.add_argument("--gen-sqlite", action="store_true", help="regenerate db/*_sqlite.sql and exit")
    args = ap.parse_args(argv)

    if args.gen_sqlite:
        for path in gen_sqlite():
            print(f"wrote {path.relative_to(REPO_ROOT).as_posix()}")
        return 0

    try:
        settings = load_settings()
        if args.backend:
            settings = dataclasses.replace(settings, db_backend=args.backend)
        with get_db(settings, sqlite_path=args.sqlite_path) as db:
            tag = f"[{db.backend}]"
            if args.reset:
                print(f"{tag} reset: dropped {len(drop_ff_objects(db))} FF_ objects")
            n_tables, n_views = create_objects(db)
            print(f"{tag} created {n_tables} tables, (re)created {n_views} views")
            if args.seed:
                counts, skipped = seed(db)
                total = sum(counts.values())
                print(f"{tag} seed: {total} rows into {len(counts)} tables"
                      + (f" ({', '.join(f'{t}={n}' for t, n in counts.items())})" if counts else " (no seed CSVs)"))
                if skipped:
                    print(f"{tag} seed: skipped files with no table or adapter: {', '.join(skipped)}")
    except Exception as exc:  # never leak host/user/password in a traceback
        print(f"error: {type(exc).__name__}: {scrub(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
