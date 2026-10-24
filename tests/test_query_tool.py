"""The zetaris_query tool: cutoff enforcement, hidden future rows, rejected statements."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.query_tool import QueryTool, SQLiteBackend, build_feed_db, validate_sql

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


if __name__ == "__main__":
    unittest.main()
