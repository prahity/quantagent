"""Offline research acceptance tests; unittest also runs without the pytest CLI.

Market prices/filings are the repository's real fixtures. External API responses
and shifted timestamps are test doubles, never demo or evaluation data.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pandas as pd
from pydantic import ValidationError

from contracts.research import Evidence, Finding, LiveCollection
from research.providers import ExaSearch, FireworksSynthesis, ProviderError
from research.service import CollectRequest, collect
from sim.engine import RunConfig
from sim.research_archive import ResearchArchive
from sim.service import SimulatorService
from sim.store import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"


def action(request, buy=False):
    return {
        "schema_version": "1.0", "run_id": request["run_id"], "request_id": request["request_id"],
        "mode": request["mode"], "window_start": request["window_start"], "window_end": request["window_end"],
        "orders": [{"order_id": "buy", "execute_at": request["window_start"], "symbol": "AAPL",
                    "side": "BUY", "quantity": 3, "order_type": "MARKET", "limit_price": None}] if buy else [],
        "reason": "research acceptance test",
    }


def collection(at):
    source = Evidence(source_id="exa:test", kind="web", symbol="AAPL", title="Test-double filing",
        content="Provider fixture; not real research.", url="https://example.com/filing", provider="exa",
        published_at=at - timedelta(days=30), available_at=at, released_at=at, retrieved_at=at)
    return LiveCollection(collection_id="research_" + "a" * 32, symbols=["AAPL", "MSFT"],
        started_at=at, available_at=at + timedelta(minutes=5), sources=[source], status="complete", search_calls=1,
        findings=[Finding(agent="risk", symbol="AAPL", statement="Fixture interpretation.",
                          source_ids=[source.source_id], interpretation=True)])


class SnapshotResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = {i: pd.read_parquet(FIXTURES / f"bars_{i}.parquet") for i in ("hourly", "daily")}
        cls.news = pd.read_parquet(FIXTURES / "news.parquet")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SnapshotStore(self.bars, self.news, "real-fixture-v1")
        self.archive = ResearchArchive(self.root / "archive")
        self.svc = SimulatorService(self.store, runs_dir=self.root / "runs", research_archive=self.archive)

    def start(self, **overrides):
        return self.svc.reset({"start": "2025-01-28", "end": "2025-01-29",
            "symbols": ["AAPL", "MSFT"], "history_lookback_days": 5, "news_lookback_days": 10, **overrides})

    def test_brief_is_emitted_with_each_decision_and_only_eligible_evidence(self):
        req = self.start()
        first = self.svc.feed_research(req["run_id"])
        while req is not None:
            brief = self.svc.feed_research(req["run_id"], req["request_id"])
            cutoff = datetime.fromisoformat(req["as_of"].replace("Z", "+00:00"))
            self.assertEqual(req["data"]["research_brief_id"], brief["brief_id"])
            self.assertEqual(req["as_of"], brief["as_of"])
            ids = {s["source_id"] for s in brief["sources"]}
            self.assertTrue(ids)
            for source in brief["sources"]:
                self.assertTrue(Evidence(**source).eligible(cutoff))
                if source["kind"] == "price":
                    for bar in json.loads(source["content"])["bars"]:
                        self.assertLessEqual(datetime.fromisoformat(bar["bar_end"].replace("Z", "+00:00")), cutoff)
            for finding in brief["findings"]:
                self.assertTrue(set(finding["source_ids"]) <= ids)
            req = self.svc.submit_action(req["run_id"], action(req))["next_request"]
        self.assertEqual(first, self.svc.feed_research(first["run_id"], first["request_id"]))
        replay = self.svc.replay(first["run_id"])
        self.assertTrue(replay["match"])
        self.assertTrue(replay["research_match"])

    def test_future_request_and_unknown_run_are_inaccessible(self):
        req = self.start()
        with self.assertRaises(KeyError):
            self.svc.feed_research(req["run_id"], "decision_99999")
        with self.assertRaises(KeyError):
            self.svc.feed_research("unknown")

    def test_new_filing_enters_loop_only_after_release(self):
        probe = self.start()
        t = pd.Timestamp(probe["as_of"])
        future = self.news.iloc[0].copy()
        future["event_id"] = "future-catalyst-test"
        future["symbol"] = "AAPL"
        future["published_at"] = t + pd.Timedelta(minutes=20)
        future["available_at"] = t + pd.Timedelta(minutes=30)
        self.svc = SimulatorService(SnapshotStore(self.bars, pd.concat([self.news, future.to_frame().T]), "shifted-test"), persist=False)
        req = self.start()
        source_id = "news:future-catalyst-test"
        self.assertNotIn(source_id, {s["source_id"] for s in self.svc.feed_research(req["run_id"])["sources"]})
        req = self.svc.submit_action(req["run_id"], action(req))["next_request"]
        self.assertIn(source_id, {s["source_id"] for s in self.svc.feed_research(req["run_id"])["sources"]})

    def test_live_retrieval_cannot_be_backdated_into_historical_run(self):
        batch = collection(datetime(2026, 10, 23, tzinfo=timezone.utc))
        self.archive.save(batch)
        req = self.start(research={"collection_id": batch.collection_id})
        brief = self.svc.feed_research(req["run_id"])
        self.assertFalse(any(s["kind"] == "web" for s in brief["sources"]))
        self.assertFalse(any(f["interpretation"] for f in brief["findings"]))
        self.assertIn("Attached live collection is unavailable at this decision time.", brief["gaps"])

    def test_collection_waits_for_synthesis_completion_and_is_pinned_for_replay(self):
        probe = self.start()
        cutoff = datetime.fromisoformat(probe["as_of"].replace("Z", "+00:00"))
        batch = collection(cutoff - timedelta(minutes=2))
        self.archive.save(batch)
        req = self.start(research={"collection_id": batch.collection_id})
        run_id = req["run_id"]
        self.assertFalse(any(s["kind"] == "web" for s in self.svc.feed_research(run_id)["sources"]))
        req = self.svc.submit_action(run_id, action(req))["next_request"]
        self.assertTrue(any(s["kind"] == "web" for s in self.svc.feed_research(run_id)["sources"]))
        while req:
            req = self.svc.submit_action(run_id, action(req))["next_request"]
        # Remove only the temporary archive to prove replay uses its per-run pinned copy.
        (self.archive.root / f"{batch.collection_id}.json").unlink()
        self.assertTrue(self.svc.replay(run_id)["research_match"])

    def test_disabling_research_preserves_same_executions_and_score(self):
        reports = []
        for enabled in (True, False):
            req = self.start(research={"enabled": enabled})
            run_id = req["run_id"]
            if not enabled:
                self.assertIsNone(req["data"]["research_brief_id"])
                self.assertIsNone(self.svc.feed_research(run_id))
                self.assertFalse((self.root / "runs" / run_id / "research.jsonl").exists())
            while req:
                result = self.svc.submit_action(run_id, action(req, buy=req["step"] == 0))
                req = result["next_request"]
            reports.append(result["report"])
        for key in ("final_cash", "final_equity", "net_profit", "fills", "rejections"):
            self.assertEqual(reports[0][key], reports[1][key])
        self.assertEqual(reports[0]["fills"], 1)

    def test_archives_and_run_artifacts_cannot_be_overwritten(self):
        batch = collection(datetime.now(timezone.utc) - timedelta(days=1))
        self.archive.save(batch)
        with self.assertRaises(FileExistsError):
            self.archive.save(batch)
        req = self.start(run_id="preserve")
        before = (self.root / "runs" / "preserve" / "research.jsonl").read_text()
        fresh = SimulatorService(self.store, runs_dir=self.root / "runs")
        with self.assertRaises(ValueError):
            fresh.reset({"run_id": "preserve", "start": "2025-01-28", "end": "2025-01-29", "symbols": ["AAPL"]})
        self.assertEqual(before, (self.root / "runs" / req["run_id"] / "research.jsonl").read_text())
        with self.assertRaises(ValidationError):
            RunConfig(run_id="../../outside", start="2025-01-28", end="2025-01-29")
        with self.assertRaises(ValueError):
            self.archive.load("../../outside")

    def test_replay_detects_missing_or_changed_research_even_when_account_matches(self):
        req = self.start()
        run_id = req["run_id"]
        while req:
            req = self.svc.submit_action(run_id, action(req))["next_request"]
        research_path = self.root / "runs" / run_id / "research.jsonl"
        lines = research_path.read_text().splitlines()
        changed = json.loads(lines[0])
        changed["gaps"].append("tampered")
        research_path.write_text(json.dumps(changed) + "\n" + "\n".join(lines[1:]) + "\n")
        report = self.svc.replay(run_id)
        self.assertTrue(report["account_match"])
        self.assertFalse(report["research_match"])
        self.assertFalse(report["match"])
        research_path.unlink()
        self.assertFalse(self.svc.replay(run_id)["research_match"])


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_exa_filters_dates_and_stamps_actual_retrieval(self):
        now = datetime.now(timezone.utc)
        base = {"title": "Example filing", "url": "https://example.com/filing", "text": "A source excerpt.",
                "publishedDate": (now - timedelta(days=1)).isoformat()}
        sent = []

        def transport(request):
            sent.append(request)
            return httpx.Response(200, json={"results": [base, {**base, "publishedDate": None},
                {**base, "publishedDate": (now + timedelta(days=1)).isoformat()},
                {**base, "url": "javascript:alert(1)"}, {**base, "text": ""}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            sources, excluded = await ExaSearch("test-key", client).search(query="filings", symbol="AAPL",
                category="financial report", now=now, lookback_days=7, max_results=5)
        self.assertEqual((len(sources), excluded), (1, 4))
        self.assertGreaterEqual(sources[0].available_at, now)
        self.assertEqual(sources[0].available_at, sources[0].retrieved_at)
        payload = json.loads(sent[0].content)
        self.assertEqual(payload["endPublishedDate"], now.isoformat())
        self.assertEqual(payload["numResults"], 5)
        self.assertEqual(sent[0].headers["x-api-key"], "test-key")

    async def test_provider_errors_do_not_expose_keys_or_response_bodies(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(429, text="secret-key"))) as client:
            with self.assertRaises(ProviderError) as error:
                await ExaSearch("secret-key", client).search(query="test", symbol=None, category="news",
                    now=datetime.now(timezone.utc), lookback_days=7, max_results=1)
        self.assertEqual(str(error.exception), "Exa returned HTTP 429.")

    async def test_fireworks_accepts_only_schema_valid_grounded_output(self):
        source = collection(datetime.now(timezone.utc) - timedelta(days=1)).sources[0]
        valid = {"findings": [{"agent": "risk", "symbol": "AAPL", "statement": "Example interpretation.",
                              "source_ids": [source.source_id], "interpretation": False}], "gaps": []}
        requests = []

        def transport(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(valid)}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result = await FireworksSynthesis("test", "pinned-test-model", client).synthesize([source], ["AAPL"])
            self.assertTrue(result.findings[0].interpretation)
            valid["findings"][0]["source_ids"] = ["invented"]
            with self.assertRaises(ProviderError):
                await FireworksSynthesis("test", "pinned-test-model", client).synthesize([source], ["AAPL"])
        self.assertEqual(requests[0]["response_format"]["type"], "json_schema")
        self.assertNotIn("tools", requests[0])
        self.assertIn("untrusted_evidence", requests[0]["messages"][1]["content"])

    async def test_malformed_and_truncated_model_output_is_rejected(self):
        source = collection(datetime.now(timezone.utc) - timedelta(days=1)).sources[0]
        for data in ({"choices": []}, {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
                     {"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _, d=data: httpx.Response(200, json=d))) as client:
                with self.assertRaises(ProviderError):
                    await FireworksSynthesis("test", "test-model", client).synthesize([source], ["AAPL"])

    async def test_collection_is_bounded_deduplicated_and_degrades_on_synthesis_failure(self):
        calls = []
        now = datetime.now(timezone.utc)

        def transport(request):
            calls.append(request)
            if request.url.host == "api.fireworks.ai":
                return httpx.Response(503, text="private provider details")
            return httpx.Response(200, json={"results": [{"title": "Test evidence", "url": "https://example.com/news",
                "text": "Test content", "publishedDate": (now - timedelta(days=1)).isoformat()}]})

        with patch.dict("os.environ", {"EXA_API_KEY": "test", "FIREWORKS_API_KEY": "test", "FIREWORKS_MODEL": "test-model"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                result = await collect(CollectRequest(symbols=["AAPL", "MSFT"], synthesize=True),
                                       {"AAPL": "Apple", "MSFT": "Microsoft"}, client)
        self.assertEqual(result.search_calls, 5)
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(result.sources), 3)  # one URL per company plus market-wide scope
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.findings, [])
        self.assertIn("Fireworks returned HTTP 503.", result.warnings)
        self.assertTrue(all(s.eligible(result.available_at) for s in result.sources))

    async def test_missing_key_and_bad_symbols_fail_before_external_calls(self):
        def unexpected(_):
            self.fail("external call was not expected")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            with patch.dict("os.environ", {"EXA_API_KEY": ""}):
                with self.assertRaises(ProviderError):
                    await collect(CollectRequest(symbols=["AAPL"]), {"AAPL": "Apple"}, client)
            with patch.dict("os.environ", {"EXA_API_KEY": "test"}):
                with self.assertRaises(ValueError):
                    await collect(CollectRequest(symbols=["UNKNOWN"]), {"AAPL": "Apple"}, client)

    async def test_transport_timeout_is_an_explicit_empty_result(self):
        def transport(request):
            raise httpx.ReadTimeout("private", request=request)

        with patch.dict("os.environ", {"EXA_API_KEY": "test"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                result = await collect(CollectRequest(symbols=["AAPL"]), {"AAPL": "Apple"}, client)
        self.assertEqual(result.status, "empty")
        self.assertEqual(len(result.sources), 0)
        self.assertEqual(result.search_calls, 3)
        self.assertTrue(all(w == "Exa timed out." for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
