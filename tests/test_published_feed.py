"""The published (Zetaris-style) feed path, without Java.

A SQLite stand-in holds the whole fixture snapshot in tables named like the Zetaris ones,
loaded with the simulator's publish rows. The runner must then give the agent exactly what
the per-run SQLite feed gives it: same orders, same query results.
"""
import json
import sqlite3
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import runner  # puts the simulator on sys.path
from agent.config import FEED_TABLES
from agent.query_tool import FeedScope, QueryTool, SQLiteBackend
from sim.publish import HIDDEN_COLUMNS, NEWS_COLUMNS, PRICE_COLUMNS, news_rows, price_rows
from tests._util import fixture_service


class PublishedSQLiteBackend(SQLiteBackend):
    """Whole-snapshot tables named like the Zetaris feed, served read-only."""

    published_feed = True

    def __init__(self, db_path, store, interval="hourly"):
        super().__init__(db_path)
        self.interval = interval
        self.data_version = store.data_version
        conn = sqlite3.connect(str(db_path))
        try:
            for name, columns, rows in (
                ("feed_prices_hourly", PRICE_COLUMNS, price_rows(store, "hourly")),
                ("feed_prices_daily", PRICE_COLUMNS, price_rows(store, "daily")),
                ("feed_news", NEWS_COLUMNS, news_rows(store)),
            ):
                cols = columns + HIDDEN_COLUMNS
                conn.execute(f"CREATE TABLE {name} ({', '.join(cols)})")
                conn.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(cols))})",
                                 [[r[c] for c in cols] for r in rows])
            conn.commit()
        finally:
            conn.close()

    def raw_source(self, table):
        return f"feed_prices_{self.interval}" if table == "prices" else "feed_news"

    def loaded_data_versions(self):
        return {self.data_version}


class PublishedFeedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_columns_match_agent_feed_tables(self):
        self.assertEqual(PRICE_COLUMNS, FEED_TABLES["prices"]["columns"])
        self.assertEqual(NEWS_COLUMNS, FEED_TABLES["news"]["columns"])

    def test_momentum_run_is_identical_on_both_backends(self):
        cfg = {"start": "2025-01-28", "end": "2025-02-04", "symbols": ["AAPL", "MSFT", "SPY"]}
        local_id, published_id = f"pub_local_{uuid.uuid4().hex[:6]}", f"pub_zet_{uuid.uuid4().hex[:6]}"
        svc = fixture_service(self.tmp / "a")
        local = runner.run("momentum", 25, {**cfg, "run_id": local_id}, service=svc, verbose=False)

        svc_b = fixture_service(self.tmp / "b")
        backend = PublishedSQLiteBackend(self.tmp / "published.sqlite", svc_b.store)
        published = runner.run("momentum", 25, {**cfg, "run_id": published_id}, service=svc_b,
                               verbose=False, backend=backend)

        for key in ("steps_done", "final_equity", "net_profit", "fills", "rejections"):
            self.assertEqual(local[key], published[key], key)
        self.assertGreater(local["fills"], 0)

        def hashes(run_id):
            log = runner.RUNS_DIR / run_id / "query_log.jsonl"
            return [json.loads(x)["result_hash"] for x in log.read_text().splitlines()]

        local_hashes = hashes(local_id)
        self.assertGreater(len(local_hashes), 0)
        self.assertEqual(local_hashes, hashes(published_id))

    def test_published_tables_are_scoped_to_the_run(self):
        svc = fixture_service(self.tmp)
        backend = PublishedSQLiteBackend(self.tmp / "published.sqlite", svc.store)
        as_of = "2025-01-29T16:30:00Z"
        scope = FeedScope(symbols=("AAPL",), from_epoch={"prices": 1738022400, "news": 0})  # 2025-01-28
        tool = QueryTool(backend, "r", "d", as_of, self.tmp / "q.jsonl", scope=scope)

        res = tool("scoped", "SELECT DISTINCT symbol, min(ts) AS first, max(released_at) AS last FROM prices GROUP BY symbol")
        self.assertTrue(res.ok, res.error)
        self.assertEqual([r["symbol"] for r in res.rows], ["AAPL"])
        self.assertGreaterEqual(res.rows[0]["first"], "2025-01-28")
        self.assertLessEqual(res.rows[0]["last"], "2025-01-29T16:30:00+00:00")

        # The raw whole-snapshot table cannot be named directly.
        raw = tool("bypass", "SELECT max(ts) FROM feed_prices_hourly")
        self.assertFalse(raw.ok)


if __name__ == "__main__":
    unittest.main()
