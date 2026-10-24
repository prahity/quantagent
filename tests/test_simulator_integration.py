"""End-to-end checks against the real simulator (fixture data slice).

Covers: the feed is never ahead of as_of during a run, a sell without inventory is rejected,
and the same scripted run twice gives identical results.
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import runner
from agent import contract
from tests._util import fixture_service


class SimulatorIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_feed_never_ahead_of_as_of_during_a_run(self):
        svc = fixture_service(self.tmp)
        req = svc.reset({"run_id": "feedchk", "start": "2025-01-28", "end": "2025-01-30",
                          "symbols": ["AAPL", "MSFT"], "history_lookback_days": 5, "news_lookback_days": 10})
        steps = 0
        while req is not None and steps < 8:
            as_of = req["as_of"]
            # runner.collect_feed asserts released_at <= as_of internally; a violation raises.
            prices, news = runner.collect_feed(svc, "feedchk", req["data"]["symbols"], as_of)
            for row in prices + news:
                self.assertLessEqual(row["released_at"], as_of)
            req = svc.submit_action("feedchk", _hold(req))["next_request"]
            steps += 1
        self.assertGreater(steps, 0)

    def test_sell_without_inventory_is_rejected(self):
        svc = fixture_service(self.tmp)
        req = svc.reset({"run_id": "sell0", "start": "2025-01-28", "end": "2025-01-29",
                          "symbols": ["AAPL"], "history_lookback_days": 5, "news_lookback_days": 10})
        order = contract.make_order(req, "s1", "AAPL", "SELL", 1)
        ack = svc.submit_action("sell0", contract.build_envelope(req, [order]))["ack"]
        self.assertEqual(ack["orders"][0]["status"], "rejected")
        self.assertIn("insufficient_inventory", ack["orders"][0]["reason"])

    def test_same_scripted_run_twice_is_identical(self):
        results = []
        for i in range(2):
            svc = fixture_service(self.tmp / f"svc{i}")
            summary = runner.run(
                "momentum", minutes=12,
                run_config={"run_id": f"det{i}", "start": "2025-01-28", "end": "2025-01-31",
                             "symbols": ["AAPL", "MSFT", "SPY"], "interval": "hourly"},
                service=svc, verbose=False,
            )
            results.append(summary)
        a, b = results
        for key in ("final_equity", "net_profit", "fills", "rejections", "steps_done"):
            self.assertEqual(a[key], b[key], f"{key} differs: {a[key]} vs {b[key]}")
        self.assertEqual(a["queries"], b["queries"])


def _hold(req: dict) -> dict:
    return {k: req[k] for k in ("schema_version", "run_id", "request_id", "mode", "window_start", "window_end")} | {
        "orders": [], "reason": "hold"}


if __name__ == "__main__":
    unittest.main()
