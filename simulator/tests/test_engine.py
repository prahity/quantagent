import json

import pandas as pd
import pytest

from sim.feed import FutureAccessError
from sim.service import SimulatorService
from tests.conftest import act, hold, make_engine, order, run_to_end


def test_first_request_matches_contract(store):
    engine = make_engine(store)
    r = engine.current_request
    assert r.as_of == pd.Timestamp("2025-01-28T14:30:00Z")
    assert r.window_end == pd.Timestamp("2025-01-28T15:30:00Z")
    assert r.account.cash == r.initial_cash == 10_000
    assert r.clock.decision_interval_seconds == 3600 and r.clock.execution_resolution_ms == 3_600_000
    assert r.data.symbols == ["AAPL", "MSFT"]
    assert r.steps_total == 28  # 4 trading days x 7 hourly bars


def test_known_order_changes_cash_by_configured_costs(store):
    engine = make_engine(store, execution={"commission_per_share": 0.01, "commission_min": 1.0, "slippage_bps": 2.0})
    r = engine.current_request
    bar_open = engine.market.open_at("AAPL", r.as_of)
    ack, nxt, _ = engine.submit_action(act(r, [order(r, "b1", "AAPL", "BUY", 10)]))
    assert ack.accepted and ack.orders[0].status == "accepted"
    fill = engine.ledger.fills[0]
    assert fill.bar_open == bar_open
    assert fill.fill_price == pytest.approx(bar_open * 1.0002)
    assert fill.commission == 1.0  # max(10 * 0.01, 1.0)
    assert engine.ledger.cash == pytest.approx(10_000 - 10 * bar_open * 1.0002 - 1.0)
    assert nxt.account.positions[0].quantity == 10
    # Marked at the close of the bar that just completed.
    close, bar_ts = engine.market.last_close_through("AAPL", nxt.as_of)
    assert bar_ts == r.as_of
    assert nxt.account.equity == pytest.approx(engine.ledger.cash + 10 * close)


def test_v2_two_orders_fill_in_order_on_next_bar_open(store):
    engine = make_engine(store, mode="V2")
    r = engine.current_request
    payload = act(r, [order(r, "buy", "MSFT", "BUY", 5, offset_s=10), order(r, "sell", "MSFT", "SELL", 5, offset_s=45)])
    ack, nxt, _ = engine.submit_action(payload)
    assert ack.accepted
    fills = engine.ledger.fills
    assert [f.order_id for f in fills] == ["buy", "sell"]
    next_open = engine.market.open_at("MSFT", nxt.as_of)
    assert all(f.filled_at == nxt.as_of.replace(tzinfo=None) or pd.Timestamp(f.filled_at) == nxt.as_of for f in fills)
    assert fills[0].bar_open == fills[1].bar_open == next_open
    assert fills[0].fill_price > next_open > fills[1].fill_price  # slippage hurts both sides
    assert nxt.account.positions == []
    assert nxt.account.cash == pytest.approx(10_000 - 5 * next_open * 2e-4)


def test_v1_order_fills_at_boundary_open_not_future_price(store):
    engine = make_engine(store)
    r = engine.current_request
    engine.submit_action(act(r, [order(r, "b", "AAPL", "BUY", 1)]))
    fill = engine.ledger.fills[0]
    assert pd.Timestamp(fill.filled_at) == r.as_of
    # The agent could not see this bar when deciding: it had not completed.
    visible = engine.market.completed_bars("AAPL", r.as_of)
    assert r.as_of not in set(visible.ts)


def test_execution_rejections_are_recorded(store):
    engine = make_engine(store, execution={"max_position_pct_equity": 0.25})
    r = engine.current_request
    ack, r, _ = engine.submit_action(act(r, [order(r, "s", "AAPL", "SELL", 1)]))
    assert ack.orders[0].status == "rejected" and ack.orders[0].reason == "insufficient_inventory"
    ack, r, _ = engine.submit_action(act(r, [order(r, "big", "AAPL", "BUY", 100_000)]))
    assert ack.orders[0].reason == "insufficient_cash"
    ack, r, _ = engine.submit_action(act(r, [order(r, "cap", "AAPL", "BUY", 20)]))  # ~4.7k > 25% of 10k
    assert ack.orders[0].reason == "position_limit"
    assert [x.reason for x in engine.ledger.rejections] == ["insufficient_inventory", "insufficient_cash", "position_limit"]
    assert engine.ledger.cash == 10_000 and engine.ledger.fills == []


def test_invalid_response_is_recorded_as_hold_and_time_advances(store):
    engine = make_engine(store)
    r = engine.current_request
    ack, nxt, _ = engine.submit_action({"garbage": True})
    assert not ack.accepted and nxt.step == 1
    ack2, _, _ = engine.submit_action(hold(r))  # stale: echoes the previous request id
    assert not ack2.accepted and {e.code for e in ack2.errors} == {"request_mismatch", "window_mismatch"}
    assert engine.invalid_responses == 2


def test_unfilled_limit_order_expires_after_validity(store):
    # validity_bars=2: may fill at the boundary it was placed on and the next two; expires at step 2.
    engine = make_engine(store, execution={"order_validity_bars": 2})
    r = engine.current_request
    ack, r, _ = engine.submit_action(act(r, [order(r, "lim", "AAPL", "BUY", 1, order_type="LIMIT", limit_price=1.0)]))
    assert ack.orders[0].status == "accepted" and len(engine.scheduler) == 1
    assert engine.ledger.rejections == []  # non-marketable is not a final rejection
    engine.submit_action(hold(r))
    assert len(engine.scheduler) == 0
    assert engine.ledger.rejections[-1].reason == "expired"
    assert engine.records[0].order_outcomes[0].status == "rejected"


def test_v1_order_expires_at_next_boundary_with_validity_one(store):
    engine = make_engine(store, execution={"order_validity_bars": 1})
    r = engine.current_request
    ack, _, _ = engine.submit_action(act(r, [order(r, "lim", "AAPL", "BUY", 1, order_type="LIMIT", limit_price=1.0)]))
    assert ack.orders[0].status == "rejected" and ack.orders[0].reason == "expired"
    assert len(engine.scheduler) == 0


def test_news_release_respects_publication_availability_and_release(store):
    # Real records: AAPL 8-K (earnings) accepted 2025-01-30 16:30:23 ET = 21:30:23Z,
    # AAPL 10-Q accepted 2025-01-31 06:01:27 ET = 11:01:27Z.
    engine = make_engine(store, start="2025-01-30", end="2025-01-31")
    eight_k, ten_q = "sec:0000320193-25-000007", "sec:0000320193-25-000008"
    request = engine.current_request
    seen = {}
    while request is not None:
        ids = set(engine.feed.news()["event_id"])
        seen[request.as_of.isoformat()] = ids
        assert ids == set(request.data.news_event_ids) or set(request.data.news_event_ids) <= ids
        _, request, _ = engine.submit_action(hold(request))
    jan30_last = seen["2025-01-30T20:30:00+00:00"]
    jan31_open = seen["2025-01-31T14:30:00+00:00"]
    assert eight_k not in jan30_last and ten_q not in jan30_last
    assert eight_k in jan31_open and ten_q in jan31_open
    assert engine.news.released_at(eight_k) == pd.Timestamp("2025-01-31T14:30:00Z")


def test_feed_refuses_future_as_of(store):
    engine = make_engine(store)
    clock = engine.feed.clock
    engine.feed.prices("AAPL", clock)
    with pytest.raises(FutureAccessError):
        engine.feed.prices("AAPL", clock + pd.Timedelta(hours=1))
    with pytest.raises(FutureAccessError):
        engine.feed.news(clock + pd.Timedelta(seconds=1))
    bars = engine.feed.prices("AAPL")
    assert (bars.bar_end <= clock).all()


def test_report_net_profit_and_valuation(store):
    engine = make_engine(store)
    r = engine.current_request

    def decide(req):
        if req.step == 0:
            return act(req, [order(req, "b", "AAPL", "BUY", 10)])
        return hold(req)

    report = run_to_end(engine, decide)
    last_close = engine.market.last_bar_close_in_run("AAPL")
    assert report.final_equity == pytest.approx(engine.ledger.cash + 10 * last_close)
    assert report.net_profit == pytest.approx(report.final_equity - 10_000)
    assert report.fills == 1 and report.steps == 28 and report.held_requests == 27
    assert report.turnover == pytest.approx(engine.ledger.fills[0].notional / 10_000)
    assert 0 <= report.max_drawdown_pct < 100


def test_daily_interval_boundaries(store):
    engine = make_engine(store, interval="daily", start="2025-01-28", end="2025-02-04", symbols=["AAPL", "SPY"])
    r = engine.current_request
    assert r.clock.interval == "daily" and r.clock.decision_interval_seconds == 86_400
    assert r.as_of == pd.Timestamp("2025-01-28T14:30:00Z")
    assert r.window_end == pd.Timestamp("2025-01-29T14:30:00Z")
    # Friday's bar completes before Monday's decision; the weekend has no boundaries.
    report = run_to_end(engine)
    assert report.steps == 6


def test_replay_reproduces_official_result(store, tmp_path):
    svc = SimulatorService(store, runs_dir=tmp_path, persist=True)
    req = svc.reset({"run_id": "rep", "start": "2025-01-28", "end": "2025-01-30", "symbols": ["AAPL", "MSFT"], "history_lookback_days": 5, "news_lookback_days": 10, "mode": "V2"})
    step = 0
    while req is not None:
        payload = hold_dict(req)
        if step == 0:
            payload["orders"] = [order_dict(req, "b", "AAPL", "BUY", 7, 10)]
        if step == 5:
            payload["orders"] = [order_dict(req, "s", "AAPL", "SELL", 7, 0)]
        result = svc.submit_action("rep", payload)
        req = result["next_request"]
        step += 1
    assert (tmp_path / "rep" / "report.json").exists()
    replay = svc.replay("rep")
    assert replay["match"] is True and replay["actions_replayed"] == step
    assert replay["original"]["fills"] == 2


def hold_dict(req: dict) -> dict:
    # Actions stay schema 1.0. The decision request is 1.1 and must not be copied onto the action.
    return {k: req[k] for k in ("run_id", "request_id", "mode", "window_start", "window_end")} | {
        "schema_version": "1.0", "orders": [], "reason": "hold"}


def order_dict(req: dict, oid: str, symbol: str, side: str, qty: int, offset_s: int) -> dict:
    at = pd.Timestamp(req["window_start"]) + pd.Timedelta(seconds=offset_s)
    return {"order_id": oid, "execute_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "symbol": symbol, "side": side, "quantity": qty, "order_type": "MARKET", "limit_price": None}


def test_run_artifacts_are_json(store, tmp_path):
    svc = SimulatorService(store, runs_dir=tmp_path, persist=True)
    req = svc.reset({"run_id": "art", "start": "2025-01-28", "end": "2025-01-28", "symbols": ["AAPL"], "history_lookback_days": 5})
    while req is not None:
        req = svc.submit_action("art", hold_dict(req))["next_request"]
    for name in ("config.json", "requests.jsonl", "actions.jsonl", "report.json", "equity.jsonl"):
        assert (tmp_path / "art" / name).exists()
    report = json.loads((tmp_path / "art" / "report.json").read_text())
    assert report["data_version"].startswith("fixture")
