"""Policy loop driven by a fake LLM: structured AgentStep steps, self-correction, last-step."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.config import PolicyConfig
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


def _decision(symbol=None, side="BUY", quantity=1, reason="ok"):
    orders = [] if symbol is None else [{"symbol": symbol, "side": side, "quantity": quantity}]
    return json.dumps({"type": "decision", "orders": orders, "reason": reason, "evidence_query_ids": []})


def _query(sql, goal="look"):
    return json.dumps({"type": "query", "goal": goal, "sql": sql})


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

    def _policy(self, model, **cfg):
        p = NemotronPolicy(model, self.dir / "workspace", architecture_text="",
                           config=PolicyConfig(**cfg) if cfg else None)
        p.start_run("run_test", stub_request("V1", as_of=AS_OF, symbols=["AAPL"]))
        return p

    def test_extract_json_handles_prose(self):
        self.assertEqual(extract_json('noise {"type":"decision","orders":[]} tail')["type"], "decision")
        self.assertIsNone(extract_json("no json here"))

    def test_valid_decision_produces_envelope(self):
        req = stub_request("V1", as_of=AS_OF, symbols=["AAPL"])
        policy = self._policy(FakeModel([_decision("AAPL", "BUY", 2, "momentum up")]))
        env = policy.decide(req, self._tool(req), self.memory)
        self.assertEqual(env["mode"], "V1")
        self.assertEqual(len(env["orders"]), 1)
        self.assertEqual(env["orders"][0]["symbol"], "AAPL")
        self.assertEqual(env["orders"][0]["quantity"], 2)
        # Code fills the fields the model never supplies.
        self.assertEqual(env["orders"][0]["order_type"], "MARKET")
        self.assertEqual(env["orders"][0]["execute_at"], req["window_start"])

    def test_query_then_decision_produces_valid_envelope(self):
        req = stub_request("V1", as_of=AS_OF, symbols=["AAPL"])
        model = FakeModel([
            _query("SELECT close FROM prices WHERE symbol='AAPL' ORDER BY ts"),
            _decision("AAPL", "BUY", 1, "momentum up"),
        ])
        policy = self._policy(model)
        tool = self._tool(req)
        env = policy.decide(req, tool, self.memory)
        self.assertEqual(len(env["orders"]), 1)
        self.assertEqual(tool.calls_used, 1)  # exactly one query turn ran
        log = [json.loads(x) for x in (self.dir / "query_log.jsonl").read_text().splitlines() if x.strip()]
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["row_count"], 2)

    def test_empty_content_then_corrected(self):
        req = stub_request("V1", as_of=AS_OF, symbols=["AAPL"])
        model = FakeModel(["", _decision("AAPL", "BUY", 1, "after retry")])
        policy = self._policy(model)
        env = policy.decide(req, self._tool(req), self.memory)
        self.assertEqual(len(env["orders"]), 1)
        # The empty/truncation correction was sent before the second call.
        self.assertTrue(any("empty or cut off" in m["content"] for m in model.calls[-1]))

    def test_invalid_then_corrected(self):
        req = stub_request("V1", as_of=AS_OF, symbols=["AAPL"])
        model = FakeModel([
            _decision("ZZZZ", "BUY", 1, "bad symbol"),   # fails contract validation
            _decision("AAPL", "BUY", 1, "fixed"),
        ])
        policy = self._policy(model)
        env = policy.decide(req, self._tool(req), self.memory)
        self.assertEqual(env["orders"][0]["symbol"], "AAPL")
        self.assertTrue(any("was invalid" in m["content"] for m in model.calls[-1]))

    def test_invalid_persists_falls_back_to_hold(self):
        req = stub_request("V1", as_of=AS_OF, symbols=["AAPL"])
        model = FakeModel([_decision("ZZZZ", "BUY", 1, "bad")] * 10)
        policy = self._policy(model)
        env = policy.decide(req, self._tool(req), self.memory)
        self.assertEqual(env["orders"], [])  # held after never producing a valid decision

    def test_last_step_is_decision_only(self):
        req = stub_request("V1", as_of=AS_OF, symbols=["AAPL"])
        model = FakeModel([
            _query("SELECT close FROM prices WHERE symbol='AAPL' ORDER BY ts"),
            _decision("AAPL", "BUY", 1, "final"),
        ])
        policy = self._policy(model, max_model_steps=2)
        env = policy.decide(req, self._tool(req), self.memory)
        self.assertEqual(len(env["orders"]), 1)
        # The last step carried the decision-only directive.
        self.assertTrue(any("No more queries" in m["content"] for m in model.calls[-1]))


if __name__ == "__main__":
    unittest.main()
