"""Policy loop driven by a fake LLM: a query turn followed by a valid decision."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.memory import Memory
from agent.policy import NemotronPolicy, extract_json
from agent.query_tool import QueryTool, SQLiteBackend, build_feed_db
from tests._util import FakeModel, stub_request

AS_OF = "2025-01-28T16:30:00Z"


def _price(symbol, hour, close):
    ts = f"2025-01-28T{hour:02d}:30:00+00:00"
    rel = f"2025-01-28T{hour:02d}:30:00+00:00"  # released <= cutoff for hours <= 16
    return {"symbol": symbol, "ts": ts, "open": close, "high": close, "low": close,
            "close": close, "volume": 100, "bar_end": rel, "released_at": rel}


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "feed.sqlite"
        build_feed_db(self.db, [_price("AAPL", 14, 100.0), _price("AAPL", 15, 101.0)], news=[])
        self.backend = SQLiteBackend(self.db)
        self.memory = Memory(self.dir / "memory.jsonl", "run_test")

    def tearDown(self):
        self._tmp.cleanup()

    def _tool(self, req):
        return QueryTool(self.backend, req["run_id"], req["request_id"], req["as_of"], self.dir / "query_log.jsonl")

    def test_extract_json_handles_prose(self):
        self.assertEqual(extract_json('noise {"type":"decision","orders":[]} tail')["type"], "decision")
        self.assertIsNone(extract_json("no json here"))

    def test_query_then_decision_produces_valid_envelope(self):
        req = stub_request("V1", as_of=AS_OF, symbols=["AAPL"])
        model = FakeModel([
            json.dumps({"type": "query", "goal": "recent closes",
                        "sql": "SELECT close FROM prices WHERE symbol='AAPL' ORDER BY ts"}),
            json.dumps({"type": "decision",
                        "orders": [{"order_id": "o1", "execute_at": req["window_start"], "symbol": "AAPL",
                                     "side": "BUY", "quantity": 1, "order_type": "MARKET", "limit_price": None}],
                        "reason": "momentum up", "evidence_query_ids": ["q1"]}),
        ])
        policy = NemotronPolicy(model, self.dir / "workspace", architecture_text="")  # no brief model call
        policy.start_run("run_test", req)
        tool = self._tool(req)
        env = policy.decide(req, tool, self.memory)

        self.assertEqual(env["mode"], "V1")
        self.assertEqual(len(env["orders"]), 1)
        self.assertEqual(env["orders"][0]["symbol"], "AAPL")
        self.assertEqual(tool.calls_used, 1)  # exactly one query turn ran
        # The query turn was logged.
        log = [json.loads(x) for x in (self.dir / "query_log.jsonl").read_text().splitlines() if x.strip()]
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["row_count"], 2)

    def test_system_prompt_names_the_sql_engine(self):
        req = stub_request("V1", as_of=AS_OF, symbols=["AAPL"])
        model = FakeModel([json.dumps({"type": "decision", "orders": [], "reason": "flat"})])
        policy = NemotronPolicy(model, self.dir / "workspace", architecture_text="")
        policy.start_run("run_test", req)
        policy.decide(req, self._tool(req), self.memory)
        system = model.calls[0][0]["content"]
        self.assertIn("SQL ENGINE: SQLite.", system)
        self.assertIn("compare them as strings", system)

    def test_invalid_decision_falls_back_to_hold(self):
        req = stub_request("V1", as_of=AS_OF, symbols=["AAPL"])
        model = FakeModel([
            json.dumps({"type": "decision",
                        "orders": [{"order_id": "o1", "execute_at": req["window_start"], "symbol": "ZZZZ",
                                     "side": "BUY", "quantity": 1, "order_type": "MARKET", "limit_price": None}],
                        "reason": "bad symbol"}),
        ] + ['{"type":"garbage"}'] * 10)
        policy = NemotronPolicy(model, self.dir / "workspace", architecture_text="")
        policy.start_run("run_test", req)
        env = policy.decide(req, self._tool(req), self.memory)
        self.assertEqual(env["orders"], [])  # held after failing to produce a valid decision


if __name__ == "__main__":
    unittest.main()
