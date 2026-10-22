"""Shared fixtures. All market/news data here is a real slice of the frozen snapshot
(AAPL, MSFT, SPY; 2025-01-27 .. 2025-02-07) written by scripts/fetch_data.py --fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sim.engine import Engine, RunConfig  # noqa: E402
from sim.store import SnapshotStore  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="session")
def store() -> SnapshotStore:
    bars = {i: pd.read_parquet(FIXTURES / f"bars_{i}.parquet") for i in ("hourly", "daily")}
    news = pd.read_parquet(FIXTURES / "news.parquet")
    return SnapshotStore(bars, news, data_version="fixture-2025-01-27..2025-02-07")


def make_engine(store: SnapshotStore, **overrides) -> Engine:
    base = {
        "run_id": overrides.pop("run_id", "t"),
        "interval": "hourly",
        "mode": "V1",
        "start": "2025-01-28",
        "end": "2025-01-31",
        "symbols": ["AAPL", "MSFT"],
        "history_lookback_days": 5,
        "news_lookback_days": 10,
        "execution": {"slippage_bps": 1.0, "commission_per_share": 0.0, "commission_min": 0.0, "max_position_pct_equity": 0.5},
    }
    execution = {**base["execution"], **overrides.pop("execution", {})}
    cfg = RunConfig(**{**base, **overrides, "execution": execution})
    engine = Engine(cfg, store)
    engine.reset()
    return engine


def hold(request) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": request.run_id,
        "request_id": request.request_id,
        "mode": request.mode,
        "window_start": request.window_start.isoformat().replace("+00:00", "Z"),
        "window_end": request.window_end.isoformat().replace("+00:00", "Z"),
        "orders": [],
        "reason": "hold",
    }


def order(request, order_id: str, symbol: str, side: str, qty: int, offset_s: int = 0, order_type: str = "MARKET", limit_price: float | None = None) -> dict:
    at = request.window_start + pd.Timedelta(seconds=offset_s)
    return {
        "order_id": order_id,
        "execute_at": at.isoformat().replace("+00:00", "Z"),
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "order_type": order_type,
        "limit_price": limit_price,
    }


def act(request, orders: list[dict]) -> dict:
    payload = hold(request)
    payload["orders"] = orders
    return payload


def run_to_end(engine: Engine, decide=None):
    """Drive the engine with ``decide(request) -> payload`` (defaults to hold) until the report."""
    request = engine.current_request
    report = None
    while request is not None:
        payload = decide(request) if decide else hold(request)
        _, request, report = engine.submit_action(payload)
    return report
