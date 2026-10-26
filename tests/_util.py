"""Shared test helpers: fixture-backed simulator service, request stubs, a fake model."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIMULATOR = ROOT / "simulator"
FIXTURES = SIMULATOR / "tests" / "fixtures"


def fixture_service(tmp_path):
    """A SimulatorService over the real fixture slice (AAPL, MSFT, SPY; Jan-Feb 2025)."""
    import pandas as pd
    from sim.service import SimulatorService
    from sim.store import SnapshotStore

    bars = {i: pd.read_parquet(FIXTURES / f"bars_{i}.parquet") for i in ("hourly", "daily")}
    news = pd.read_parquet(FIXTURES / "news.parquet")
    store = SnapshotStore(bars, news, data_version="fixture-test")
    return SimulatorService(store, runs_dir=Path(tmp_path) / "_sim", persist=False)


def stub_request(mode: str = "V1", as_of: str = "2025-01-28T14:30:00Z", symbols=None) -> dict:
    """A minimal but contract-shaped decision request for unit tests that need no simulator."""
    symbols = symbols or ["AAPL", "MSFT"]
    window_end = "2025-01-28T15:30:00Z"
    return {
        "schema_version": "1.0",
        "run_id": "run_test",
        "request_id": "decision_00000",
        "mode": mode,
        "as_of": as_of,
        "window_start": as_of,
        "window_end": window_end,
        "initial_cash": 10000,
        "account": {"currency": "USD", "cash": 10000, "equity": 10000, "positions": []},
        "clock": {"cadence_clock": "simulated", "decision_interval_seconds": 3600,
                   "time_scale": 60, "execution_resolution_ms": 3_600_000, "interval": "hourly"},
        "limits": {"max_orders_per_request": 20, "max_position_pct_equity": 0.25,
                    "allow_shorting": False, "order_validity_bars": 1,
                    "slippage_bps": 1.0, "commission_per_share": 0.0},
        "data": {"feed_schema": "feed_run_test", "workspace_schema": "agent_run_test",
                  "news_event_ids": [], "symbols": symbols},
        "step": 0,
        "steps_total": 28,
    }


class FakeModel:
    """Deterministic stand-in for NemotronModel. Returns scripted responses in order."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    def chat(self, messages, response_json: bool = False, guided_json=None) -> str:
        self.calls.append([dict(m) for m in messages])
        if not self._responses:
            return '{"type":"decision","orders":[],"reason":"no script left"}'
        return self._responses.pop(0)

    def chat_full(self, messages, response_json: bool = False, guided_json=None):
        from agent.model import ModelResult

        content = self.chat(messages, response_json=response_json, guided_json=guided_json)
        # Empty content stands in for a max_tokens cut-off so tests exercise the retry path.
        finish_reason = "length" if content == "" else "stop"
        return ModelResult(
            content=content, raw_content=content, finish_reason=finish_reason,
            status=200, retries=0, latency_ms=0.0,
            prompt_tokens=None, completion_tokens=None, total_tokens=None, reasoning=None,
        )
