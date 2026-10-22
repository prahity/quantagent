"""HTTP-level checks for the FastAPI runner layer, using the fixture snapshot
so no network or full snapshot is needed."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from sim.service import SimulatorService


@pytest.fixture
def client(store, tmp_path):
    app_module._service = SimulatorService(store, runs_dir=tmp_path, persist=False)
    try:
        yield TestClient(app_module.app)
    finally:
        app_module._service = None


def _create(client, **overrides):
    body = {"start": "2025-01-28", "end": "2025-01-31", "symbols": ["AAPL", "MSFT"], "history_lookback_days": 5, "news_lookback_days": 10}
    body.update(overrides)
    res = client.post("/api/runs", json=body)
    assert res.status_code == 201, res.text
    request = res.json()
    return request["run_id"], request


def test_manual_market_order_over_http_fills(client):
    run_id, req = _create(client, label="http order")
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "request_id": req["request_id"],
        "mode": "V1",
        "window_start": req["window_start"],
        "window_end": req["window_end"],
        "orders": [{"order_id": "o1", "execute_at": req["window_start"], "symbol": "AAPL", "side": "BUY", "quantity": 3, "order_type": "MARKET", "limit_price": None}],
        "reason": "manual",
    }
    res = client.post(f"/api/runs/{run_id}/actions", json=payload)
    assert res.status_code == 200, res.text
    ack = res.json()["ack"]
    assert ack["accepted"] is True and ack["errors"] == []
    assert ack["orders"][0]["status"] == "accepted"

    fills = client.get(f"/api/runs/{run_id}/fills").json()
    assert len(fills) == 1 and fills[0]["symbol"] == "AAPL" and fills[0]["quantity"] == 3


def test_missing_body_is_recorded_as_rejected_hold(client):
    run_id, _ = _create(client)
    res = client.post(f"/api/runs/{run_id}/actions")
    assert res.status_code == 200
    ack = res.json()["ack"]
    assert ack["accepted"] is False
    assert ack["errors"][0]["code"] == "not_an_object"


def test_feed_refuses_future_as_of(client):
    run_id, _ = _create(client)
    res = client.get(f"/api/runs/{run_id}/feed/prices", params={"symbol": "AAPL", "as_of": "2025-02-05T14:30:00Z"})
    assert res.status_code == 403
