"""Analysis queries. The agent names `bars` and `filings`. The backend binds `as_of`.

The default backend is the Zetaris JDBC lake (`ANALYSIS_BACKEND=zetaris`, or unset).
`bars` is `feed_prices_daily` UNION `feed_prices_hourly`; `filings` is `feed_news`.
The cutoff is a server-side CTE on `_released_epoch`, so the model's SQL cannot move it.
`ANALYSIS_BACKEND=sqlite` reads the simulator snapshot instead (the without-Zetaris arm).

`ZetarisSource` is the unused HTTP client (`ZETARIS_QUERY_URL`). It is not the default.
Memory is not stored here.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
_QUALIFIED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")
_DEFAULT_DAILY = "aitrader_lake.feed.feed_prices_daily"
_DEFAULT_HOURLY = "aitrader_lake.feed.feed_prices_hourly"
_DEFAULT_NEWS = "aitrader_lake.feed.feed_news"
_DEFAULT_MANIFEST = "aitrader_lake.feed.feed_manifest"
# Names the model must not reach. The CTE below is the only way into the lake.
_HIDDEN_SQL = re.compile(
    r"feed_prices|feed_news|feed_manifest|aitrader_lake|_released|data_version|price_history|filing_history",
    re.IGNORECASE,
)

SCHEMA = """Relations already limited to records available at this decision's as_of. A date filter cannot reveal later rows.
bars(symbol, interval, ts, open, high, low, close, volume)
filings(symbol, form, title, published_at, available_at)
features(symbol, close, ret_5, ret_21, ret_63, vol_21, ma_gap_21, ma_gap_63, dd_63, rs_spy, days_since_filing, days_until_filing, last_filing, next_filing)
interval is 'daily' or 'hourly'. Times are UTC text, YYYY-MM-DDTHH:MM:SSZ.
ret_5, ret_21 and ret_63 are the close-to-close return over that many released sessions. vol_21 is the volatility of the last 21 daily returns. ma_gap_21 and ma_gap_63 are the close divided by that moving average, minus 1. dd_63 is the drawdown from the 63-session high. rs_spy is ret_21 minus SPY's ret_21. days_since_filing and days_until_filing count from filings already released by as_of. A feature is null when fewer released sessions exist than the window. The cutoff is applied before this view is built.
Reply with one SELECT, or WITH ... SELECT, reading only bars, filings, and features.
"""

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NOMINAL_SECONDS = {"daily": 86400, "hourly": 3600}
_ROW_LIMIT = 30
_ALLOWED_READ = {"bars", "filings", "features"}
_FEATURES_NAME = re.compile(r"\bfeatures\b", re.IGNORECASE)
_ALLOWED_ACTION = {
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
    sqlite3.SQLITE_TRANSACTION,
}


@dataclass
class QueryRequest:
    """One analysis call. `as_of` is the simulator cutoff. The model does not set it."""

    run_id: str
    request_id: str
    query_id: str
    goal: str
    sql: str
    as_of: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryResult:
    query_id: str
    goal: str
    sql: str
    as_of: str
    backend: str
    data_version: str
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool
    error: str
    elapsed_ms: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def text(self) -> str:
        if self.error:
            return f"error: {self.error}"
        if not self.rows:
            return "(no rows)"
        header = "\t".join(self.columns)
        body = "\n".join("\t".join(_cell(row.get(col)) for col in self.columns) for row in self.rows)
        note = "\n(truncated)" if self.truncated else ""
        return f"{header}\n{body}{note}"


class AnalysisSource(Protocol):
    name: str
    schema_text: str
    data_version: str

    def execute(self, request: QueryRequest) -> QueryResult: ...


def utc_text(value: Any) -> str:
    """Fixed-width UTC text. String comparison matches time order."""
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cell(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return "" if value is None else str(value)


def attach_feature_view(source: Any, request: QueryRequest) -> QueryRequest:
    """Replace a query that reads `features` with the rows released at its as_of.

    The view is built by the cutoff query, then inlined. The agent's SQL cannot
    move that cutoff, and a short history stays null.
    """
    if getattr(source, "_binding_features", False) or not _FEATURES_NAME.search(request.sql or ""):
        return request
    source._binding_features = True
    try:
        from meterless.strategy import bind_features, load_features

        rows = load_features(source, request.as_of, list(getattr(source, "feature_symbols", None) or []))
        sql = bind_features(request.sql, rows)
    finally:
        source._binding_features = False
    return QueryRequest(
        run_id=request.run_id,
        request_id=request.request_id,
        query_id=request.query_id,
        goal=request.goal,
        sql=sql,
        as_of=request.as_of,
        parameters=dict(request.parameters),
    )


def one_select(sql: str) -> str:
    text = sql.strip().rstrip(";").strip()
    if not text or ";" in text:
        raise ValueError("send one SELECT")
    head = text.lstrip("(").lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise ValueError("send one SELECT")
    return text


def _authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, source: str | None) -> int:
    if action not in _ALLOWED_ACTION:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and arg1 not in _ALLOWED_READ and arg1 is not None:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _with_bar_end(bars: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in bars:
        groups.setdefault((str(row["interval"]), str(row["symbol"])), []).append(row)
    ready = []
    for (interval, _symbol), rows in groups.items():
        nominal = _NOMINAL_SECONDS[interval]
        ordered = sorted(rows, key=lambda item: utc_text(item["ts"]))
        for index, row in enumerate(ordered):
            start = datetime.strptime(utc_text(row["ts"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            end = start + timedelta(seconds=nominal)
            if index + 1 < len(ordered):
                nxt = datetime.strptime(utc_text(ordered[index + 1]["ts"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if nxt < end:
                    end = nxt
            ready.append({
                "symbol": row["symbol"],
                "interval": interval,
                "ts": utc_text(start),
                "bar_end": utc_text(end),
                "open": round(float(row["open"]), 4),
                "high": round(float(row["high"]), 4),
                "low": round(float(row["low"]), 4),
                "close": round(float(row["close"]), 4),
                "volume": int(row["volume"]),
            })
    return ready


def _result(request: QueryRequest, backend: str, data_version: str, started: float, **kwargs: Any) -> QueryResult:
    return QueryResult(
        query_id=request.query_id,
        goal=request.goal,
        sql=request.sql,
        as_of=request.as_of,
        backend=backend,
        data_version=data_version,
        columns=kwargs.get("columns", []),
        rows=kwargs.get("rows", []),
        truncated=kwargs.get("truncated", False),
        error=kwargs.get("error", ""),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )


class SqliteSource:
    """Local stand-in for the Zetaris released views. Same relation names, same cutoff rule."""

    name = "sqlite"
    schema_text = SCHEMA

    def __init__(self, path: Path, data_version: str):
        self.path = Path(path)
        self.data_version = data_version
        self.feature_symbols: list[str] = []

    @classmethod
    def build(cls, path: Path, bars: list[dict], filings: list[dict], data_version: str = "local") -> "SqliteSource":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        conn = sqlite3.connect(path)
        try:
            conn.execute("""
                CREATE TABLE price_history (
                    symbol TEXT, interval TEXT, ts TEXT, bar_end TEXT,
                    open REAL, high REAL, low REAL, close REAL, volume INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE filing_history (
                    symbol TEXT, form TEXT, title TEXT, published_at TEXT, available_at TEXT
                )
            """)
            conn.executemany(
                "INSERT INTO price_history VALUES (?,?,?,?,?,?,?,?,?)",
                [tuple(row[k] for k in ("symbol", "interval", "ts", "bar_end", "open", "high", "low", "close", "volume")) for row in _with_bar_end(bars)],
            )
            conn.executemany(
                "INSERT INTO filing_history VALUES (?,?,?,?,?)",
                [(row["symbol"], row.get("form") or "", str(row.get("title") or "")[:200], utc_text(row["published_at"]), utc_text(row["available_at"])) for row in filings],
            )
            conn.execute("CREATE INDEX idx_price_end ON price_history(bar_end)")
            conn.execute("CREATE INDEX idx_filing_avail ON filing_history(available_at)")
            conn.commit()
        finally:
            conn.close()
        return cls(path, data_version)

    @classmethod
    def open_snapshot(cls, snapshot_dir: Path, db_path: Path | None = None) -> "SqliteSource":
        snapshot_dir = Path(snapshot_dir)
        db_path = Path(db_path) if db_path else snapshot_dir / "analysis.sqlite"
        import pandas as pd

        files = [snapshot_dir / name for name in ("bars_daily.parquet", "bars_hourly.parquet", "news.parquet")]
        missing = [path for path in files[:2] if not path.exists()]
        if missing:
            raise FileNotFoundError(f"missing {missing[0]}. Run simulator/scripts/fetch_data.py")
        newest = max(path.stat().st_mtime for path in files if path.exists())
        manifest_path = snapshot_dir.parent.parent / "config" / "snapshot_manifest.json"
        data_version = "unpinned"
        if manifest_path.exists():
            data_version = json.loads(manifest_path.read_text()).get("data_version", data_version)
        if not db_path.exists() or db_path.stat().st_mtime < newest:
            frames = [pd.read_parquet(path) for path in files[:2]]
            bars = pd.concat(frames, ignore_index=True).to_dict(orient="records")
            filings = pd.read_parquet(files[2]).to_dict(orient="records") if files[2].exists() else []
            cls.build(db_path, bars, filings, data_version)
        return cls(db_path, data_version)

    def execute(self, request: QueryRequest, row_limit: int | None = None) -> QueryResult:
        request = attach_feature_view(self, request)
        started = time.perf_counter()
        try:
            statement = one_select(request.sql)
            cutoff = utc_text(request.as_of)
        except (ValueError, TypeError) as exc:
            return _result(request, self.name, self.data_version, started, error=str(exc))
        cap = _ROW_LIMIT if row_limit is None else int(row_limit)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("DROP TABLE IF EXISTS temp.bars")
            conn.execute("DROP TABLE IF EXISTS temp.filings")
            conn.execute("""
                CREATE TEMP TABLE bars AS
                SELECT symbol, interval, ts, open, high, low, close, volume
                FROM price_history WHERE bar_end <= ?
            """, (cutoff,))
            conn.execute("""
                CREATE TEMP TABLE filings AS
                SELECT symbol, form, title, published_at, available_at
                FROM filing_history WHERE published_at <= ? AND available_at <= ?
            """, (cutoff, cutoff))
            conn.set_authorizer(_authorizer)
            try:
                cursor = conn.execute(f"SELECT * FROM ({statement}) LIMIT ?", (cap + 1,))
                fetched = cursor.fetchall()
            except sqlite3.Error as exc:
                return _result(request, self.name, self.data_version, started, error=str(exc))
            finally:
                conn.set_authorizer(None)
            truncated = len(fetched) > cap
            kept = fetched[:cap]
            columns = [item[0] for item in cursor.description or []]
            rows = [{key: row[key] for key in row.keys()} for row in kept]
            return _result(request, self.name, self.data_version, started, columns=columns, rows=rows, truncated=truncated)
        finally:
            conn.close()


class ZetarisSource:
    """Same `execute` contract against a Zetaris SQL endpoint.

    Register the released objects as `released_bars` and `released_filings`,
    or set `ZETARIS_BARS_VIEW` and `ZETARIS_FILINGS_VIEW`. Those names must
    differ from `bars` and `filings`, which are the names the agent uses.
    This class copies the cutoff into the query it sends. The agent's SQL stays
    the SQL it would send to the local backend.
    """

    name = "zetaris"
    schema_text = SCHEMA

    def __init__(self, query_url: str, token: str = "", bars_view: str = "released_bars", filings_view: str = "released_filings", data_version: str = "zetaris", client: httpx.Client | None = None):
        for label, name in (("bars view", bars_view), ("filings view", filings_view)):
            if not _IDENT.match(name) or name in _ALLOWED_READ:
                raise ValueError(f"{label} must be an identifier other than bars or filings")
        self.query_url = query_url
        self.token = token
        self.bars_view = bars_view
        self.filings_view = filings_view
        self.data_version = data_version
        self.feature_symbols: list[str] = []
        self._client = client or httpx.Client(timeout=30)

    @classmethod
    def from_env(cls) -> "ZetarisSource":
        url = os.environ.get("ZETARIS_QUERY_URL", "").strip()
        if not url:
            raise RuntimeError("ANALYSIS_BACKEND=zetaris requires ZETARIS_QUERY_URL")
        return cls(
            url,
            token=os.environ.get("ZETARIS_TOKEN", ""),
            bars_view=os.environ.get("ZETARIS_BARS_VIEW", "released_bars"),
            filings_view=os.environ.get("ZETARIS_FILINGS_VIEW", "released_filings"),
            data_version=os.environ.get("ZETARIS_DATA_VERSION", "zetaris"),
        )

    def prepare(self, sql: str, as_of: str) -> str:
        statement = one_select(sql)
        cutoff = utc_text(as_of)
        for hidden in (self.bars_view, self.filings_view):
            if re.search(rf"\b{re.escape(hidden)}\b", statement, re.IGNORECASE):
                raise ValueError("query the released relations bars and filings")
        bound = (
            f"bars AS (SELECT symbol, interval, ts, open, high, low, close, volume FROM {self.bars_view} WHERE bar_end <= '{cutoff}'), "
            f"filings AS (SELECT symbol, form, title, published_at, available_at FROM {self.filings_view} "
            f"WHERE published_at <= '{cutoff}' AND available_at <= '{cutoff}')"
        )
        if statement.lower().startswith("with"):
            return f"WITH {bound}, {statement[4:].lstrip()}"
        return f"WITH {bound} {statement}"

    def execute(self, request: QueryRequest) -> QueryResult:
        request = attach_feature_view(self, request)
        started = time.perf_counter()
        try:
            statement = self.prepare(request.sql, request.as_of)
        except (ValueError, TypeError) as exc:
            return _result(request, self.name, self.data_version, started, error=str(exc))
        headers = {"authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            response = self._client.post(self.query_url, headers=headers, json={
                "sql": statement,
                "as_of": utc_text(request.as_of),
                "query_id": request.query_id,
                "goal": request.goal,
                "run_id": request.run_id,
                "request_id": request.request_id,
            })
        except httpx.HTTPError as exc:
            return _result(request, self.name, self.data_version, started, error=f"zetaris did not answer: {exc}")
        if response.status_code != 200:
            return _result(request, self.name, self.data_version, started, error=f"zetaris returned HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            return _result(request, self.name, self.data_version, started, error="zetaris response was not an object")
        if payload.get("error"):
            return _result(request, self.name, self.data_version, started, error=str(payload["error"]))
        columns = [str(col) for col in payload.get("columns") or []]
        raw_rows = payload.get("rows") or []
        rows: list[dict[str, Any]] = []
        for row in raw_rows[:_ROW_LIMIT]:
            if isinstance(row, dict):
                rows.append(row)
            elif isinstance(row, list):
                rows.append({columns[i] if i < len(columns) else str(i): value for i, value in enumerate(row)})
        if not columns and rows:
            columns = list(rows[0].keys())
        return _result(
            request, self.name, self.data_version, started,
            columns=columns, rows=rows, truncated=bool(payload.get("truncated")) or len(raw_rows) > _ROW_LIMIT,
        )


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_REPO_ROOT / ".env")


def _qualified(name: str, label: str) -> str:
    if not _QUALIFIED.match(name):
        raise ValueError(f"{label} must be a dotted table name, not {name!r}")
    return name


def _manifest_version(snapshot_dir: Path | None) -> str | None:
    """data_version pinned in the simulator manifest, when that file is present."""
    if snapshot_dir is None:
        return None
    path = Path(snapshot_dir).parent.parent / "config" / "snapshot_manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("data_version")


def _plain(value: Any) -> Any:
    """JDBC values to JSON-friendly Python. JPype boxes subclass int/float and unbox."""
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _match_sqlite_cell(column: str, value: Any) -> Any:
    """Round and reformat so a lake row compares equal to SqliteSource's row."""
    value = _plain(value)
    if value is None:
        return "" if column in ("form", "title", "symbol") else None
    if column in ("open", "high", "low", "close") and isinstance(value, (int, float)):
        return round(float(value), 4)
    if column == "volume" and isinstance(value, (int, float)):
        return int(value)
    if column == "title":
        return str(value)[:200]
    if column in ("ts", "published_at", "available_at"):
        try:
            return utc_text(value)
        except (TypeError, ValueError):
            return str(value)
    return value


class JdbcZetarisSource:
    """Analysis queries against the published Zetaris JDBC lake.

    The model still names `bars` and `filings`. This class maps those onto
    `feed_prices_daily` / `feed_prices_hourly` and `feed_news`, and wraps every
    statement in a CTE whose `_released_epoch <= as_of` predicate the model
    cannot edit. The manifest must hold exactly one data_version.
    """

    name = "zetaris"
    schema_text = SCHEMA

    def __init__(
        self,
        daily_table: str = _DEFAULT_DAILY,
        hourly_table: str = _DEFAULT_HOURLY,
        news_table: str = _DEFAULT_NEWS,
        manifest_table: str = _DEFAULT_MANIFEST,
        *,
        url: str = "",
        driver: str = "",
        jar: str = "",
        user: str = "",
        password: str = "",
        expected_version: str | None = None,
        connect: bool = True,
    ):
        self.daily_table = _qualified(daily_table, "daily prices")
        self.hourly_table = _qualified(hourly_table, "hourly prices")
        self.news_table = _qualified(news_table, "filings")
        self.manifest_table = _qualified(manifest_table, "manifest")
        self._url = url
        self._driver = driver
        self._jar = jar
        self._user = user
        self._password = password
        self.data_version = ""
        self.feature_symbols: list[str] = []
        self._conn = None
        if connect:
            self._require_login()
            self._check_version(expected_version)

    @classmethod
    def from_env(cls, expected_version: str | None = None) -> "JdbcZetarisSource":
        _load_dotenv()
        missing = [name for name in ("ZETARIS_JDBC_URL", "ZETARIS_JDBC_DRIVER", "ZETARIS_AGENT_USER") if not os.environ.get(name)]
        if missing:
            raise RuntimeError(f"Zetaris JDBC lake missing config: {', '.join(missing)}")
        return cls(
            os.environ.get("ZETARIS_FEED_PRICES_DAILY") or _DEFAULT_DAILY,
            os.environ.get("ZETARIS_FEED_PRICES_HOURLY") or _DEFAULT_HOURLY,
            os.environ.get("ZETARIS_FEED_NEWS") or _DEFAULT_NEWS,
            os.environ.get("ZETARIS_FEED_MANIFEST") or _DEFAULT_MANIFEST,
            url=os.environ["ZETARIS_JDBC_URL"],
            driver=os.environ["ZETARIS_JDBC_DRIVER"],
            jar=os.environ.get("ZETARIS_JDBC_JAR") or "",
            user=os.environ["ZETARIS_AGENT_USER"],
            password=os.environ.get("ZETARIS_AGENT_PASSWORD") or "",
            expected_version=expected_version,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _require_login(self) -> None:
        missing = [name for name, value in (("url", self._url), ("driver", self._driver), ("user", self._user)) if not value]
        if missing:
            raise RuntimeError(f"Zetaris JDBC lake missing config: {', '.join(missing)}")

    def _connection(self):
        if self._conn is None:
            import jaydebeapi  # lazy: SQLite runs need no Java

            self._conn = jaydebeapi.connect(self._driver, self._url, [self._user, self._password], self._jar or None)
        return self._conn

    def _fetch(self, sql: str) -> tuple[list[str], list[tuple]]:
        cur = self._connection().cursor()
        try:
            cur.execute(sql)
            columns = [str(item[0]).lower() for item in cur.description] if cur.description else []
            return columns, list(cur.fetchall())
        finally:
            cur.close()

    def _check_version(self, expected: str | None) -> None:
        try:
            _columns, rows = self._fetch(f"SELECT data_version FROM {self.manifest_table}")
        except Exception as exc:
            raise RuntimeError(f"Zetaris manifest {self.manifest_table} could not be read: {exc}") from exc
        found = {str(_plain(row[0])) for row in rows if row and row[0] is not None}
        if len(found) != 1:
            raise RuntimeError(f"Zetaris lake must hold exactly one data_version (found {sorted(found)})")
        self.data_version = next(iter(found))
        if expected and self.data_version != expected:
            raise RuntimeError(
                f"Zetaris lake data_version {self.data_version!r} does not match the simulator snapshot {expected!r}. "
                "Reload with simulator/scripts/publish_zetaris.py --replace."
            )

    def prepare(self, sql: str, as_of: str, row_limit: int | None = None) -> str:
        """Wrap one agent statement. The cutoff literal is not part of the agent's SQL."""
        statement = one_select(sql)
        if _HIDDEN_SQL.search(statement):
            raise ValueError("query the released relations bars and filings")
        self._row_limit = _ROW_LIMIT if row_limit is None else int(row_limit)
        cutoff = int(datetime.fromisoformat(utc_text(as_of).replace("Z", "+00:00")).timestamp())
        bars = (
            "bars AS ("
            f"SELECT symbol, 'daily' AS interval, ts, open, high, low, close, volume "
            f"FROM {self.daily_table} WHERE _released_epoch <= {cutoff} "
            "UNION ALL "
            f"SELECT symbol, 'hourly' AS interval, ts, open, high, low, close, volume "
            f"FROM {self.hourly_table} WHERE _released_epoch <= {cutoff})"
        )
        filings = (
            "filings AS ("
            "SELECT symbol, form, title, published_at, available_at "
            f"FROM {self.news_table} WHERE _released_epoch <= {cutoff})"
        )
        bound = f"{bars}, {filings}"
        if statement.lower().startswith("with"):
            wrapped = f"WITH {bound}, {statement[4:].lstrip()}"
        else:
            wrapped = f"WITH {bound} {statement}"
        return f"SELECT * FROM ({wrapped}) LIMIT {self._row_limit + 1}"

    def execute(self, request: QueryRequest, row_limit: int | None = None) -> QueryResult:
        request = attach_feature_view(self, request)
        started = time.perf_counter()
        try:
            statement = self.prepare(request.sql, request.as_of, row_limit)
        except (ValueError, TypeError) as exc:
            return _result(request, self.name, self.data_version, started, error=str(exc))
        try:
            columns, fetched = self._fetch(statement)
        except Exception as exc:
            return _result(request, self.name, self.data_version, started, error=str(exc).splitlines()[0][-300:])
        cap = _ROW_LIMIT if row_limit is None else int(row_limit)
        truncated = len(fetched) > cap
        rows = []
        for raw in fetched[:cap]:
            rows.append({columns[i]: _match_sqlite_cell(columns[i], raw[i]) for i in range(len(columns))})
        return _result(request, self.name, self.data_version, started, columns=columns, rows=rows, truncated=truncated)


def open_analysis_source(backend: str | None = None, snapshot_dir: Path | None = None, *, expected_version: str | None = None) -> AnalysisSource:
    """`zetaris` (the default) is the JDBC lake. `sqlite` is the local comparison backend."""
    _load_dotenv()
    chosen = (backend if backend is not None else os.environ.get("ANALYSIS_BACKEND", "zetaris")).strip().lower()
    if chosen == "sqlite":
        if snapshot_dir is None:
            raise ValueError("sqlite backend needs the simulator snapshot directory")
        return SqliteSource.open_snapshot(snapshot_dir)
    if chosen == "zetaris":
        pinned = expected_version if expected_version is not None else _manifest_version(snapshot_dir)
        return JdbcZetarisSource.from_env(expected_version=pinned)
    raise ValueError(f"unknown analysis backend {chosen}")
