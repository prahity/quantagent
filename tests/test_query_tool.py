"""The zetaris_query tool: cutoff enforcement, hidden future rows, rejected statements."""
import json
import os
import time
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agent.query_tool import FeedScope, QueryTool, SQLiteBackend, ZetarisJDBCBackend, build_feed_db, validate_sql

AS_OF = "2025-01-28T16:30:00Z"  # 16:30


def _price(symbol, hour, close, released_hour):
    ts = f"2025-01-28T{hour:02d}:30:00+00:00"
    rel = f"2025-01-28T{released_hour:02d}:30:00+00:00"
    return {"symbol": symbol, "ts": ts, "open": close, "high": close, "low": close,
            "close": close, "volume": 100, "bar_end": rel, "released_at": rel}


class QueryToolTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "feed.sqlite"
        prices = [
            _price("AAPL", 14, 100.0, 15),   # released 15:30  <= cutoff -> visible
            _price("AAPL", 15, 101.0, 16),   # released 16:30  == cutoff -> visible
            _price("AAPL", 16, 102.0, 17),   # released 17:30  >  cutoff -> HIDDEN
            _price("AAPL", 17, 103.0, 18),   # released 18:30  >  cutoff -> HIDDEN
        ]
        build_feed_db(self.db, prices, news=[])
        self.backend = SQLiteBackend(self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self):
        return QueryTool(self.backend, "run_test", "decision_00000", AS_OF, self.dir / "query_log.jsonl")

    def test_future_rows_are_hidden(self):
        res = self.tool()("all prices", "SELECT ts, close, released_at FROM prices ORDER BY ts")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.row_count, 2)
        self.assertTrue(all(r["released_at"] <= AS_OF for r in res.rows))
        closes = sorted(r["close"] for r in res.rows)
        self.assertEqual(closes, [100.0, 101.0])

    def test_agent_cannot_move_the_cutoff(self):
        # Even explicitly asking for later-released rows returns nothing past the cutoff.
        res = self.tool()("try future", "SELECT close FROM prices WHERE released_at > :t", {"t": AS_OF})
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.row_count, 0)
        # Reserved cutoff parameter is refused.
        res2 = self.tool()("inject", "SELECT 1", {"_as_of_epoch": 9999999999})
        self.assertFalse(res2.ok)
        self.assertIn("_", res2.error)

    def test_raw_tables_are_unreachable(self):
        res = self.tool()("bypass", "SELECT * FROM _prices")
        self.assertFalse(res.ok)

    def test_rejects_write_and_dangerous_statements(self):
        bad = [
            "INSERT INTO prices VALUES (1)",
            "DROP TABLE prices",
            "SELECT * FROM prices; ATTACH DATABASE 'x.db' AS y",
            "PRAGMA table_info(prices)",
            "SELECT * FROM sqlite_master",
            "SELECT * FROM main.prices",
            "UPDATE prices SET close = 0",
            "DELETE FROM prices",
        ]
        for sql in bad:
            res = self.tool()(f"reject {sql}", sql)
            self.assertFalse(res.ok, f"should have rejected: {sql}")

    def test_allows_plain_select_and_with(self):
        self.assertEqual(validate_sql("SELECT close FROM prices"), [])
        self.assertEqual(validate_sql("WITH x AS (SELECT close FROM prices) SELECT * FROM x"), [])

    def test_table_allowlist_blocks_raw_published_tables(self):
        # On Zetaris the raw tables hold the whole snapshot; naming them must be refused.
        bad = [
            "SELECT * FROM aitrader_lake.feed.feed_prices_hourly",
            "SELECT * FROM feed.feed_news",
            "SELECT * FROM feed_prices_hourly",
            "SELECT p.close FROM prices p JOIN feed_news n ON n.symbol = p.symbol",
            "SELECT * FROM prices WHERE symbol IN (SELECT symbol FROM ds.other)",
            "SELECT * FROM read_files('s3://bucket/x')",
            "SELECT * FROM parquet.`s3://bucket/x`",
            "WITH prices AS (SELECT 1 AS close) SELECT close FROM prices",
            "SELECT FROM WHERE",
        ]
        for sql in bad:
            self.assertNotEqual(validate_sql(sql), [], f"should have rejected: {sql}")

    def test_table_allowlist_keeps_normal_analysis(self):
        good = [
            "SELECT p.symbol, p.close FROM prices p JOIN news n ON n.symbol = p.symbol WHERE p.ts > :t",
            "WITH r AS (SELECT symbol, close FROM prices), m AS (SELECT symbol, max(close) c FROM r GROUP BY symbol) SELECT * FROM m",
            "SELECT (SELECT max(close) FROM prices) AS hi, count(*) FROM news",
            "SELECT symbol, strftime('%Y-%m-%d', ts) AS d FROM prices ORDER BY ts DESC LIMIT 5",
        ]
        for sql in good:
            self.assertEqual(validate_sql(sql), [], sql)

    def test_row_limit_and_truncation(self):
        tool = QueryTool(self.backend, "r", "d", AS_OF, self.dir / "q.jsonl")
        tool.config = type(tool.config)(row_limit=1)
        res = tool("limited", "SELECT close FROM prices")
        self.assertEqual(res.row_count, 1)
        self.assertTrue(res.truncated)

    def test_per_decision_call_budget(self):
        tool = QueryTool(self.backend, "r", "d", AS_OF, self.dir / "q.jsonl")
        tool.config = type(tool.config)(calls_per_decision=1)
        self.assertTrue(tool("one", "SELECT close FROM prices").ok)
        over = tool("two", "SELECT close FROM prices")
        self.assertFalse(over.ok)
        self.assertIn("budget", over.error)

    def test_every_call_is_logged(self):
        log = self.dir / "query_log.jsonl"
        t = QueryTool(self.backend, "run_test", "decision_00000", AS_OF, log)
        t("ok", "SELECT close FROM prices")
        t("bad", "DROP TABLE prices")
        lines = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
        self.assertEqual(len(lines), 2)
        for entry in lines:
            for field in ("query_id", "goal", "sql", "cutoff", "row_count", "truncation", "error", "ms", "result_hash"):
                self.assertIn(field, entry)
        self.assertEqual(lines[0]["cutoff"], AS_OF)
        self.assertIsNotNone(lines[1]["error"])


_ZETARIS_ENV = {
    "ZETARIS_JDBC_URL": "jdbc:zetaris:lightning@example:10000",
    "ZETARIS_JDBC_DRIVER": "com.zetaris.lightning.jdbc.LightningDriver",
    "ZETARIS_AGENT_USER": "aitrader_agent",
    "ZETARIS_FEED_PRICES_HOURLY": "lake.feed.feed_prices_hourly",
    "ZETARIS_FEED_PRICES_DAILY": "lake.feed.feed_prices_daily",
    "ZETARIS_FEED_NEWS": "lake.feed.feed_news",
    "ZETARIS_FEED_MANIFEST": "lake.feed.feed_manifest",
}


class _FakeStatement:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeCursor:
    """Stands in for a jaydebeapi cursor; `delay` simulates a slow remote query."""

    def __init__(self, rows, delay=0.0):
        self.rows, self.delay = rows, delay
        self.description = [("close",), ("n",)]
        self._prep = _FakeStatement()
        self.executed = None

    def execute(self, sql, params):
        self.executed = (sql, list(params))
        deadline = time.monotonic() + self.delay
        while time.monotonic() < deadline and not self._prep.cancelled:
            time.sleep(0.01)
        if self._prep.cancelled:
            raise RuntimeError("statement cancelled")

    def fetchmany(self, n):
        return self.rows[:n]

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class ZetarisBackendTest(unittest.TestCase):
    def _backend(self, cursor, interval="hourly"):
        with mock.patch.dict(os.environ, _ZETARIS_ENV):
            backend = ZetarisJDBCBackend(interval)
        backend._conn = _FakeConn(cursor)
        return backend

    def test_interval_selects_the_price_table(self):
        self.assertEqual(self._backend(None, "daily").raw_source("prices"), "lake.feed.feed_prices_daily")
        self.assertEqual(self._backend(None).raw_source("news"), "lake.feed.feed_news")

    def test_missing_config_is_reported(self):
        with mock.patch.dict(os.environ, {**_ZETARIS_ENV, "ZETARIS_FEED_NEWS": ""}):
            with self.assertRaises(RuntimeError) as ctx:
                ZetarisJDBCBackend()
        self.assertIn("ZETARIS_FEED_NEWS", str(ctx.exception))

    def test_named_params_become_ordered_qmarks_and_values_are_plain(self):
        cur = _FakeCursor([(Decimal("101.5"), 3), (Decimal("99.25"), 4)])
        cols, rows, truncated = self._backend(cur).run(
            "SELECT * FROM t WHERE a <= :x AND b = :y AND c >= :x", {"x": 7, "y": "AAPL"}, 1, 2.0)
        self.assertEqual(cur.executed, ("SELECT * FROM t WHERE a <= ? AND b = ? AND c >= ?", [7, "AAPL", 7]))
        self.assertEqual((cols, rows, truncated), (["close", "n"], [(101.5, 3)], True))

    def test_slow_query_is_cancelled_at_the_deadline(self):
        cur = _FakeCursor([], delay=5.0)
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            self._backend(cur).run("SELECT 1", {}, 10, 0.2)
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertTrue(cur._prep.cancelled)

    def test_tool_binds_scope_and_cutoff_for_zetaris(self):
        cur = _FakeCursor([])
        scope = FeedScope(symbols=("AAPL", "MSFT"), from_epoch={"prices": 100, "news": 50})
        with TemporaryDirectory() as tmp:
            tool = QueryTool(self._backend(cur), "r", "d", AS_OF, Path(tmp) / "q.jsonl", scope=scope)
            res = tool("scoped", "SELECT close FROM prices")
        self.assertTrue(res.ok, res.error)
        sql, params = cur.executed
        self.assertIn("FROM lake.feed.feed_prices_hourly WHERE _released_epoch <= ?", sql)
        self.assertIn("FROM lake.feed.feed_news WHERE", sql)
        cutoff = int(datetime.fromisoformat(AS_OF.replace("Z", "+00:00")).timestamp())
        self.assertEqual(params, [cutoff, 100, "AAPL", "MSFT", cutoff, 50, "AAPL", "MSFT"])


if __name__ == "__main__":
    unittest.main()
