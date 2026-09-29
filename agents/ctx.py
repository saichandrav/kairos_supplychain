"""Runtime settings and the DB wrapper shared by every agent (plan §2, §7).

Settings come only from the environment (optionally a gitignored `.env`).
`get_db()` returns a `Db` with the same API for HANA Cloud and the SQLite
offline fallback; both use `?` placeholders.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import pandas as pd
from dotenv import load_dotenv

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SQLITE_PATH = REPO_ROOT / "data" / "kairos.sqlite"


@dataclass(frozen=True)
class Settings:
    """Values of the plan §7 environment variables. Secrets are hidden from repr."""

    db_backend: str = "sqlite"
    hana_host: str = ""
    hana_port: int = 443
    hana_user: str = ""
    hana_password: str = field(default="", repr=False)
    hana_schema: str = ""
    sap_api_hub_key: str = field(default="", repr=False)
    llm_provider: str = "none"
    llm_model: str = ""
    anthropic_api_key: str = field(default="", repr=False)
    openrouter_api_key: str = field(default="", repr=False)
    openrouter_model: str = "openai/gpt-4o-mini"
    searoutes_api_key: str = field(default="", repr=False)
    searoutes_base_url: str = "https://api.searoutes.com"
    app_api_key: str = field(default="", repr=False)
    newsapi_key: str = field(default="", repr=False)


def load_settings(dotenv: bool = True) -> Settings:
    """Read settings from the environment; `.env` values never override real env vars."""
    if dotenv:
        load_dotenv(override=False)
    env = os.environ.get
    backend = (env("DB_BACKEND") or "sqlite").strip().lower()
    if backend not in ("hana", "sqlite"):
        raise ValueError(f"DB_BACKEND must be 'hana' or 'sqlite', got {backend!r}")
    return Settings(
        db_backend=backend,
        hana_host=env("HANA_HOST", ""),
        hana_port=int(env("HANA_PORT") or 443),
        hana_user=env("HANA_USER", ""),
        hana_password=env("HANA_PASSWORD", ""),
        hana_schema=env("HANA_SCHEMA", "") or env("HANA_USER", ""),
        sap_api_hub_key=env("SAP_API_HUB_KEY", ""),
        llm_provider=(env("LLM_PROVIDER") or "none").strip().lower(),
        llm_model=env("LLM_MODEL", ""),
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        openrouter_api_key=env("OPENROUTER_API_KEY", ""),
        openrouter_model=env("OPENROUTER_MODEL", "") or "openai/gpt-4o-mini",
        searoutes_api_key=env("SEAROUTES_API_KEY", ""),
        searoutes_base_url=env("SEAROUTES_BASE_URL", "") or "https://api.searoutes.com",
        app_api_key=env("APP_API_KEY", ""),
        newsapi_key=env("NEWSAPI_KEY", ""),
    )


class Db:
    """Thin DB-API wrapper: query() -> DataFrame, execute(), executemany().

    Autocommits after every write so a crash never leaves a half-open
    transaction on the shared HANA instance; `with db.transaction():` groups
    writes into one commit and rolls them all back on error.
    """

    def __init__(self, conn: Any, backend: str):
        self._conn = conn
        self.backend = backend
        self._in_tx = False
        # hdbcli connections autocommit by default: an explicit commit() would only cost a round trip
        self._autocommit = bool(getattr(conn, "getautocommit", lambda: False)())

    @contextmanager
    def transaction(self) -> Iterator["Db"]:
        """Group writes into one commit (all or nothing) instead of committing after each statement."""
        if self._in_tx:
            yield self
            return
        self._in_tx = True
        if self._autocommit:
            self._conn.setautocommit(False)
        try:
            yield self
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            if self._autocommit:
                self._conn.setautocommit(True)
            self._in_tx = False

    def _commit(self) -> None:
        if not (self._in_tx or self._autocommit):
            self._conn.commit()

    def query(self, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, tuple(params or ()))
            cols = [d[0] for d in cur.description or ()]
            return pd.DataFrame.from_records(cur.fetchall(), columns=cols)
        finally:
            cur.close()

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, tuple(params or ()))
            self._commit()
            return cur.rowcount
        finally:
            cur.close()

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        rows = [tuple(r) for r in rows]
        if not rows:
            return 0
        cur = self._conn.cursor()
        try:
            cur.executemany(sql, rows)
            self._commit()
            return len(rows)
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Db":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _connect_hana(s: Settings) -> Any:
    from hdbcli import dbapi  # imported lazily so sqlite-only setups never need it

    if not (s.hana_host and s.hana_user and s.hana_password):
        raise RuntimeError("DB_BACKEND=hana needs HANA_HOST, HANA_USER and HANA_PASSWORD in env")
    host = s.hana_host.split(":")[0].strip() if ":" in s.hana_host else s.hana_host.strip()
    kwargs: dict[str, Any] = dict(
        address=host,
        port=s.hana_port,
        user=s.hana_user,
        password=s.hana_password,
        encrypt=True,
        sslValidateCertificate=True,
    )
    if s.hana_schema:
        kwargs["currentSchema"] = s.hana_schema
    log.info("connecting to HANA host=%s port=%s schema=%s", s.hana_host, s.hana_port, s.hana_schema)
    return dbapi.connect(**kwargs)


def get_db(settings: Settings | None = None, sqlite_path: str | Path | None = None) -> Db:
    """Open a connection for the configured backend.

    `sqlite_path` overrides data/kairos.sqlite (tests pass a tmp file or ":memory:").
    """
    s = settings or load_settings()
    if s.db_backend == "hana":
        return Db(_connect_hana(s), "hana")
    path = str(sqlite_path) if sqlite_path is not None else str(SQLITE_PATH)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return Db(sqlite3.connect(path), "sqlite")
