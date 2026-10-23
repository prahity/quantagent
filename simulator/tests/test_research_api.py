"""HTTP contract checks; require the project's FastAPI dependency."""
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.test_research import collection

HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None
if HAS_FASTAPI:
    import pandas as pd
    from fastapi.testclient import TestClient
    import server.app as app_module
    from sim.research_archive import ResearchArchive
    from sim.service import SimulatorService
    from sim.store import SnapshotStore


@unittest.skipUnless(HAS_FASTAPI, "FastAPI is not installed")
class ResearchApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        fixtures = Path(__file__).parent / "fixtures"
        store = SnapshotStore({i: pd.read_parquet(fixtures / f"bars_{i}.parquet") for i in ("hourly", "daily")},
                              pd.read_parquet(fixtures / "news.parquet"), "fixture")
        self.archive = ResearchArchive(root / "archive")
        self.service_patch = patch.object(app_module, "_service", SimulatorService(store, runs_dir=root / "runs", research_archive=self.archive))
        self.archive_patch = patch.object(app_module, "_research_archive", self.archive)
        self.service_patch.start()
        self.archive_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.addCleanup(self.archive_patch.stop)
        self.client = TestClient(app_module.app)
        self.addCleanup(self.client.close)
        self.run_body = {"start": "2025-01-28", "end": "2025-01-29", "symbols": ["AAPL", "MSFT"]}

    def test_research_updates_with_actions_and_future_briefs_are_unavailable(self):
        response = self.client.post("/api/runs", json=self.run_body)
        self.assertEqual(response.status_code, 201)
        req = response.json()
        path = f"/api/runs/{req['run_id']}/research"
        first = self.client.get(path).json()["brief"]
        self.assertEqual(first["brief_id"], req["data"]["research_brief_id"])
        self.assertEqual(self.client.get(path, params={"request_id": "decision_99999"}).status_code, 404)
        result = self.client.post(f"/api/runs/{req['run_id']}/autoplay", json={"agent": "hold", "steps": 1})
        self.assertEqual(result.status_code, 200)
        current = self.client.get(path).json()["brief"]
        self.assertNotEqual(current["brief_id"], first["brief_id"])
        self.assertEqual(self.client.get(path, params={"request_id": first["request_id"]}).json()["brief"], first)

    def test_research_can_be_disabled(self):
        req = self.client.post("/api/runs", json={**self.run_body, "research": {"enabled": False}}).json()
        self.assertIsNone(self.client.get(f"/api/runs/{req['run_id']}/research").json()["brief"])

    def test_collection_authentication_prevents_external_requests(self):
        with patch("server.app.collect", new_callable=AsyncMock) as mocked:
            with patch.dict("os.environ", {"RESEARCH_API_TOKEN": ""}):
                self.assertEqual(self.client.post("/api/research/collections", json={"symbols": ["AAPL"]}).status_code, 503)
            with patch.dict("os.environ", {"RESEARCH_API_TOKEN": "operator-secret"}):
                self.assertEqual(self.client.post("/api/research/collections", json={"symbols": ["AAPL"]}).status_code, 401)
                self.assertEqual(self.client.get("/api/research/collections/research_" + "a" * 32).status_code, 401)
            mocked.assert_not_called()

    def test_collection_persists_and_status_never_returns_credentials(self):
        batch = collection(datetime.now(timezone.utc) - timedelta(days=1))
        with patch.dict("os.environ", {"RESEARCH_API_TOKEN": "operator-secret", "EXA_API_KEY": "vendor-secret"}), \
             patch("server.app.collect", new_callable=AsyncMock, return_value=batch):
            headers = {"Authorization": "Bearer operator-secret"}
            response = self.client.post("/api/research/collections", json={"symbols": ["AAPL"]}, headers=headers)
            self.assertEqual(response.status_code, 201)
            saved = self.client.get(f"/api/research/collections/{batch.collection_id}", headers=headers)
            self.assertEqual(saved.json(), response.json())
            status = self.client.get("/api/research/providers")
            self.assertTrue(status.json()["exa_configured"])
            self.assertNotIn("vendor-secret", status.text)
            self.assertNotIn("operator-secret", status.text)

    def test_invalid_config_and_budget_are_rejected(self):
        with patch.dict("os.environ", {"RESEARCH_API_TOKEN": "operator-secret"}), \
             patch("server.app.collect", new_callable=AsyncMock) as mocked:
            headers = {"Authorization": "Bearer operator-secret"}
            for body in ({"symbols": ["AAPL"], "max_results": 100},
                         {"symbols": ["AAPL"], "as_of": "2025-01-28T14:30:00Z"},
                         {"symbols": ["AAPL", "MSFT", "NVDA"]}):
                self.assertEqual(self.client.post("/api/research/collections", json=body, headers=headers).status_code, 422)
            mocked.assert_not_called()
        response = self.client.post("/api/runs", json={**self.run_body, "research": {"collection_id": "../../private"}})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
