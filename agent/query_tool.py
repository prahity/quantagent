"""The `zetaris_query` tool: the agent's only read path into the released feed.

The RUNNER constructs one tool per decision, bound to that decision's run_id, request_id
and server-controlled `as_of`. The agent calls `tool(goal, sql, params)` and can never move
the cutoff: `as_of` is not a parameter of the call and every feed table is exposed only
through a CTE already filtered by `released_at <= as_of`.

Enforcement (defence in depth):
  * SELECT / WITH only, single statement.
  * Only the released feed tables (`prices`, `news`) are reachable; raw/underscore tables,
    schema-qualified names (`main.`, `temp.`), and `sqlite_*` are rejected.
  * Write / DDL / ATTACH / PRAGMA keywords rejected.
  * Row limit, per-query timeout, and a per-decision call budget.
  * SQLite backend uses a read-only connection plus an authorizer; the Zetaris backend
    uses the separate ZETARIS_AGENT_USER identity and the database's own permissions.

Every call is logged to runs/<run_id>/query_log.jsonl.

This module imports neither the simulator nor any model; it is pure data access.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent.config import FEED_TABLES, QueryConfig

# ------------------------------------------------------------------ SQL guard rails
_FORBIDDEN_KEYWORDS = re.compile(
    r"(?i)\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|"
    r"vacuum|reindex|analyze|grant|revoke|truncate|trigger|upsert|merge)\b"
)
_SCHEMA_QUALIFIED = re.compile(r"(?i)\b(main|temp|temporary)\s*\.")
_SQLITE_INTERNAL = re.compile(r"(?i)sqlite_")
# An identifier that starts with an underscore (blocks the raw _prices/_news/_as_of tables).
_UNDERSCORE_IDENT = re.compile(r"""(?i)(?:^|[^a-z0-9_."'])_[a-z0-9_]+""")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

_AS_OF_PARAM = "_as_of_epoch"


def _strip_comments(sql: str) -> str:
    return _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql))


def _iso_to_epoch(ts: str) -> int:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def validate_sql(sql: str) -> list[str]:
    """Return a list of reasons the SQL is rejected; empty means it passed the guards."""
    errors: list[str] = []
    cleaned = _strip_comments(sql).strip()
    if not cleaned:
        return ["empty query"]

    # Single statement: nothing after a trailing semicolon.
    body = cleaned[:-1] if cleaned.endswith(";") else cleaned
    if ";" in body:
        errors.append("only a single statement is allowed")

    head = body.lstrip().lower()
    if not (head.startswith("select") or head.startswith("with")):
        errors.append("only SELECT / WITH queries are allowed")

    if _FORBIDDEN_KEYWORDS.search(body):
        errors.append("write / DDL / ATTACH / PRAGMA statements are not allowed")
    if _SQLITE_INTERNAL.search(body):
        errors.append("references to sqlite_* objects are not allowed")
    if _SCHEMA_QUALIFIED.search(body):
        errors.append("schema-qualified names (main./temp.) are not allowed")
    if _UNDERSCORE_IDENT.search(body):
        errors.append("only the released feed tables may be read")
    return errors


@dataclass
class QueryResult:
    query_id: str
    goal: str
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    error: str | None = None
    ms: float = 0.0
    result_hash: str | None = None

    def to_log(self, run_id: str, request_id: str, cutoff: str, sql: str) -> dict:
        return {
            "query_id": self.query_id,
            "run_id": run_id,
            "request_id": request_id,
            "goal": self.goal,
            "sql": sql,
            "cutoff": cutoff,
            "row_count": self.row_count,
            "truncation": self.truncated,
            "error": self.error,
            "ms": round(self.ms, 3),
            "result_hash": self.result_hash,
        }


# ------------------------------------------------------------------ backends
class QueryBackend(ABC):
    """Executes already-validated, CTE-wrapped SQL. Two implementations share this API."""

    @abstractmethod
    def raw_source(self, table: str) -> str:
        """Backend identifier for the released-feed table behind an agent-facing name."""

    @abstractmethod
    def run(self, sql: str, params: dict, row_limit: int, timeout_s: float) -> tuple[list[str], list[tuple], bool]:
        """Return (columns, rows, truncated). `rows` holds up to `row_limit` rows."""

    def close(self) -> None:  # pragma: no cover - optional
        pass


class SQLiteBackend(QueryBackend):
    """Read-only SQLite over the per-run released feed built by the runner."""

    # sqlite3 authorizer action codes.
    _SQLITE_SELECT = 21
    _SQLITE_READ = 20
    _SQLITE_FUNCTION = 31
    _SQLITE_RECURSIVE = 33
    _OK, _DENY = 0, 1

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def raw_source(self, table: str) -> str:
        return f"_{table}"

    def _authorizer(self, action, arg1, arg2, dbname, source):
        if action in (self._SQLITE_SELECT, self._SQLITE_FUNCTION, self._SQLITE_RECURSIVE):
            return self._OK
        if action == self._SQLITE_READ:
            # arg1 is the table name; deny internal catalogues.
            if arg1 and str(arg1).lower().startswith("sqlite_"):
                return self._DENY
            return self._OK
        return self._DENY  # ATTACH, PRAGMA, every write/DDL action

    def run(self, sql: str, params: dict, row_limit: int, timeout_s: float):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=timeout_s)
        try:
            conn.row_factory = sqlite3.Row
            deadline = time.monotonic() + timeout_s

            def _progress():
                return 1 if time.monotonic() > deadline else 0

            conn.set_progress_handler(_progress, 1000)
            conn.set_authorizer(self._authorizer)
            cur = conn.execute(sql, params)
            fetched = cur.fetchmany(row_limit + 1)
            truncated = len(fetched) > row_limit
            rows = fetched[:row_limit]
            columns = [c[0] for c in cur.description] if cur.description else []
            return columns, [tuple(r) for r in rows], truncated
        finally:
            conn.close()


class ZetarisJDBCBackend(QueryBackend):
    """Zetaris over JDBC using the separate ZETARIS_AGENT_USER identity.

    Requires `jaydebeapi` (and a Java runtime). Enforcement relies on this read-only
    agent identity and the database's own row/column permissions in addition to the
    shared SQL guards and CTE cutoff. Only used when QUERY_BACKEND=zetaris.
    """

    def __init__(self) -> None:
        self._url = os.environ.get("ZETARIS_JDBC_URL") or ""
        self._driver = os.environ.get("ZETARIS_JDBC_DRIVER") or ""
        self._jar = os.environ.get("ZETARIS_JDBC_JAR") or ""
        self._user = os.environ.get("ZETARIS_AGENT_USER") or ""
        self._password = os.environ.get("ZETARIS_AGENT_PASSWORD") or ""
        self._sources = {
            "prices": os.environ.get("ZETARIS_FEED_PRICES") or "",
            "news": os.environ.get("ZETARIS_FEED_NEWS") or "",
        }
        missing = [k for k, v in {
            "ZETARIS_JDBC_URL": self._url,
            "ZETARIS_JDBC_DRIVER": self._driver,
            "ZETARIS_AGENT_USER": self._user,
            "ZETARIS_FEED_PRICES": self._sources["prices"],
            "ZETARIS_FEED_NEWS": self._sources["news"],
        }.items() if not v]
        if missing:
            raise RuntimeError(f"Zetaris backend missing config: {', '.join(missing)}")
        self._conn = None

    def raw_source(self, table: str) -> str:
        return self._sources[table]

    def _connect(self):
        if self._conn is None:
            import jaydebeapi  # imported lazily so SQLite runs need no Java

            self._conn = jaydebeapi.connect(
                self._driver, self._url, [self._user, self._password],
                self._jar or None,
            )
        return self._conn

    def run(self, sql: str, params: dict, row_limit: int, timeout_s: float):
        # Named params -> qmark in appearance order (jaydebeapi uses the DB-API qmark style).
        ordered: list = []

        def _sub(match):
            ordered.append(params[match.group(1)])
            return "?"

        qmark_sql = re.sub(r":([a-zA-Z_][a-zA-Z0-9_]*)", _sub, sql)
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(qmark_sql, ordered)
            fetched = cur.fetchmany(row_limit + 1)
            truncated = len(fetched) > row_limit
            rows = fetched[:row_limit]
            columns = [d[0] for d in cur.description] if cur.description else []
            return columns, [tuple(r) for r in rows], truncated
        finally:
            cur.close()

    def close(self) -> None:  # pragma: no cover - needs a live instance
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# ------------------------------------------------------------------ the tool
class QueryTool:
    """Per-decision, cutoff-bound query interface the agent and baselines call."""

    def __init__(
        self,
        backend: QueryBackend,
        run_id: str,
        request_id: str,
        as_of: str,
        log_path: str | Path,
        config: QueryConfig | None = None,
    ):
        self.backend = backend
        self.run_id = run_id
        self.request_id = request_id
        self.as_of = as_of
        self._as_of_epoch = _iso_to_epoch(as_of)
        self.log_path = Path(log_path)
        self.config = config or QueryConfig()
        self._calls = 0

    @property
    def calls_used(self) -> int:
        return self._calls

    def _cte_prefix(self) -> str:
        parts = []
        for name, spec in FEED_TABLES.items():
            cols = ", ".join(spec["columns"])
            src = self.backend.raw_source(name)
            parts.append(f"{name} AS (SELECT {cols} FROM {src} WHERE _released_epoch <= :{_AS_OF_PARAM})")
        return "WITH " + ",\n     ".join(parts)

    def _wrap(self, sql: str) -> str:
        cleaned = _strip_comments(sql).strip().rstrip(";").strip()
        prefix = self._cte_prefix()
        head = cleaned.lstrip().lower()
        if head.startswith("with"):
            # Merge the agent's own CTEs after ours.
            return prefix + ",\n     " + cleaned[cleaned.lower().index("with") + 4:].lstrip()
        return prefix + "\n" + cleaned

    def __call__(self, goal: str, sql: str, params: dict | None = None) -> QueryResult:
        self._calls += 1
        qid = f"q_{self.run_id}_{self.request_id}_{self._calls:03d}"
        params = dict(params or {})
        start = time.perf_counter()

        result = self._execute(qid, goal, sql, params)
        result.ms = (time.perf_counter() - start) * 1000.0
        self._log(result, sql)
        return result

    def _execute(self, qid: str, goal: str, sql: str, params: dict) -> QueryResult:
        if self._calls > self.config.calls_per_decision:
            return QueryResult(qid, goal, ok=False, error="per-decision query budget exceeded")

        reserved = [k for k in params if k.startswith("_")]
        if reserved:
            return QueryResult(qid, goal, ok=False, error=f"parameter names may not start with '_': {reserved}")

        sql_errors = validate_sql(sql)
        if sql_errors:
            return QueryResult(qid, goal, ok=False, error="; ".join(sql_errors))

        wrapped = self._wrap(sql)
        bind = {**params, _AS_OF_PARAM: self._as_of_epoch}
        try:
            columns, raw_rows, truncated = self.backend.run(
                wrapped, bind, self.config.row_limit, self.config.timeout_s
            )
        except Exception as exc:  # surfaced as a structured error, never raised at the agent
            return QueryResult(qid, goal, ok=False, error=f"{type(exc).__name__}: {exc}")

        rows = [dict(zip(columns, r)) for r in raw_rows]
        digest = hashlib.sha256(
            json.dumps({"columns": columns, "rows": rows}, sort_keys=True, default=str).encode()
        ).hexdigest()
        return QueryResult(
            qid, goal, ok=True, columns=columns, rows=rows,
            row_count=len(rows), truncated=truncated, result_hash=digest,
        )

    def _log(self, result: QueryResult, sql: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = result.to_log(self.run_id, self.request_id, self.as_of, sql)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")


# ------------------------------------------------------------------ feed db builder
def build_feed_db(db_path: str | Path, prices: list[dict], news: list[dict]) -> None:
    """(Re)build the per-run released-feed SQLite database from plain row dicts.

    Called by the runner, which collects released rows from the simulator feed. Each raw
    table carries a hidden `_released_epoch` used by the CTE cutoff; the agent-facing
    columns are exactly those in FEED_TABLES. This function imports no simulator code.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        for name, spec in FEED_TABLES.items():
            cols = spec["columns"]
            raw = f"_{name}"
            coldefs = ", ".join(f'"{c}"' for c in cols)
            conn.execute(f"DROP TABLE IF EXISTS {raw}")
            conn.execute(f"CREATE TABLE {raw} ({coldefs}, _released_epoch INTEGER)")
            source = prices if name == "prices" else news
            if source:
                placeholders = ", ".join(["?"] * (len(cols) + 1))
                insert = f"INSERT INTO {raw} ({coldefs}, _released_epoch) VALUES ({placeholders})"
                rows = []
                for row in source:
                    released = row.get(spec["released_col"])
                    epoch = _iso_to_epoch(released) if released is not None else 0
                    rows.append([row.get(c) for c in cols] + [epoch])
                conn.executemany(insert, rows)
        conn.commit()
    finally:
        conn.close()


def make_backend(db_path: str | Path | None = None, backend: str | None = None) -> QueryBackend:
    """Factory used by the runner. Defaults to SQLite over the per-run feed db."""
    backend = (backend or os.environ.get(QueryConfig().backend_env) or "sqlite").strip().lower()
    if backend == "zetaris":
        return ZetarisJDBCBackend()
    if db_path is None:
        raise ValueError("SQLite backend needs a db_path")
    return SQLiteBackend(db_path)
