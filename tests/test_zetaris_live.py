"""Live checks against the configured Zetaris instance. Skipped unless ZETARIS_LIVE=1.

Needs .env filled in (see .env.example), a JDK, and a published feed
(simulator/scripts/publish_zetaris.py, or --fixtures for the small slice).
"""
import json
import os
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import runner
from agent.query_tool import QueryTool, ZetarisJDBCBackend

LIVE = os.environ.get("ZETARIS_LIVE") == "1"


@unittest.skipUnless(LIVE, "set ZETARIS_LIVE=1 to run against the Zetaris instance")
class ZetarisLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = ZetarisJDBCBackend("hourly")
        versions = cls.backend.loaded_data_versions()
        assert len(versions) == 1, f"expected one published data_version, found {sorted(versions)}"
        cls.data_version = versions.pop()
        cls.data = "fixtures" if cls.data_version.startswith("fixture") else "snapshot"

    @classmethod
    def tearDownClass(cls):
        cls.backend.close()

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cutoff_hides_future_rows(self):
        as_of = "2025-01-29T16:30:00Z"
        tool = QueryTool(self.backend, "live", "d0", as_of, self.tmp / "q.jsonl")
        res = tool("latest released", "SELECT max(released_at) AS last, count(*) AS n FROM prices")
        self.assertTrue(res.ok, res.error)
        self.assertLessEqual(res.rows[0]["last"], "2025-01-29T16:30:00+00:00")

    def test_raw_table_is_refused_before_reaching_zetaris(self):
        tool = QueryTool(self.backend, "live", "d0", "2025-01-29T16:30:00Z", self.tmp / "q.jsonl")
        raw = tool("bypass", f"SELECT max(ts) FROM {os.environ['ZETARIS_FEED_PRICES_HOURLY']}")
        self.assertFalse(raw.ok)

    def test_momentum_matches_the_local_backend(self):
        cfg = {"start": "2025-01-28", "end": "2025-01-31"}
        a, b = f"live_sqlite_{uuid.uuid4().hex[:6]}", f"live_zetaris_{uuid.uuid4().hex[:6]}"
        local = runner.run("momentum", 12, {**cfg, "run_id": a}, data=self.data, backend_kind="sqlite", verbose=False)
        remote = runner.run("momentum", 12, {**cfg, "run_id": b}, data=self.data, backend_kind="zetaris", verbose=False)
        for key in ("steps_done", "final_equity", "net_profit", "fills", "rejections"):
            self.assertEqual(local[key], remote[key], key)

        def hashes(run_id):
            log = runner.RUNS_DIR / run_id / "query_log.jsonl"
            return [json.loads(x)["result_hash"] for x in log.read_text().splitlines()]

        self.assertEqual(hashes(a), hashes(b))


if __name__ == "__main__":
    unittest.main()
