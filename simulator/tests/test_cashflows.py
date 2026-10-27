"""Deposits and cash interest are simulator-owned. Omitted config stays at zero."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.scripted import BuyAndHoldAgent, CashOnlyAgent
from runner import LocalFeedReader, run_agent
from sim.metrics import excess_sharpe, max_drawdown_pct
from sim.service import SimulatorService
from tests.conftest import act, hold, make_engine, order, run_to_end

ROOT = Path(__file__).resolve().parents[2]


def test_request_schema_is_1_1_and_old_config_adds_no_cash(store):
    engine = make_engine(store)
    request = engine.current_request
    assert request.schema_version == "1.1"
    assert request.account.deposit_today == 0
    assert request.account.total_deposits == 0
    assert request.account.interest_earned == 0
    assert request.account.cash_annual_rate == 0
    assert request.account.cash == 10_000
    report = run_to_end(engine)
    assert engine.ledger.cash_events == []
    assert report.total_deposits == 0 and report.interest_earned == 0
    assert report.net_profit == pytest.approx(report.final_equity - 10_000)
    assert report.time_weighted_return_pct == pytest.approx(report.return_pct)
    # The action the agent sends is still schema 1.0.
    assert hold(engine.current_request or request)["schema_version"] == "1.0"


def test_deposit_is_credited_once_per_trading_day(store):
    engine = make_engine(store, daily_deposit=200, cash_annual_rate=0, run_id="dep")
    by_day: dict = {}
    request = engine.current_request
    report = None
    while request is not None:
        day = request.as_of.date()
        by_day.setdefault(day, []).append(request)
        _, request, report = engine.submit_action(hold(request))

    deposits = [event for event in engine.ledger.cash_events if event.kind == "deposit"]
    assert len(by_day) > 1
    assert len(deposits) == len(by_day)
    assert len({event.trading_day for event in deposits}) == len(by_day)
    assert engine.ledger.interest_earned == 0
    for day, requests in by_day.items():
        assert requests[0].account.deposit_today == 200
        assert requests[0].account.cash_annual_rate == 0
        for later in requests[1:]:
            assert later.account.deposit_today == 0
            assert later.account.cash == pytest.approx(requests[0].account.cash)
    assert report.total_deposits == pytest.approx(200 * len(by_day))
    assert report.final_equity == pytest.approx(10_000 + report.total_deposits)
    assert report.net_profit == pytest.approx(0)
    assert report.net_profit == pytest.approx(report.final_equity - 10_000 - report.total_deposits)
    assert report.time_weighted_return_pct == pytest.approx(0)
    assert len(report.daily_cash) == len(by_day)


def test_interest_compounds_on_cash_once_per_trading_day(store):
    engine = make_engine(store, cash_annual_rate=0.05, run_id="rate")
    request = engine.current_request
    first = True
    credited = None
    while request is not None:
        if request.account.interest_earned > 0 and credited is None:
            credited = request
        payload = act(request, [order(request, "b", "AAPL", "BUY", 10)]) if first else hold(request)
        first = False
        _, request, report = engine.submit_action(payload)

    interests = [event for event in engine.ledger.cash_events if event.kind == "interest"]
    days = {event.trading_day for event in interests}
    assert len(interests) == len(days)
    assert len(interests) < report.steps  # once per day, not once per hourly bar
    for event in interests:
        cash_before = event.cash_after - event.amount
        assert event.amount == pytest.approx(cash_before * 0.05 / 252)

    assert credited is not None
    interest = credited.account.interest_earned
    cash_before = credited.account.cash - interest
    equity_before = credited.account.equity - interest
    assert cash_before < equity_before  # the open AAPL position is not in the cash balance
    assert interest == pytest.approx(cash_before * 0.05 / 252)
    assert interest != pytest.approx(equity_before * 0.05 / 252)

    assert report.interest_earned == pytest.approx(sum(event.amount for event in interests))
    assert report.total_deposits == 0
    assert report.net_profit == pytest.approx(report.final_equity - 10_000)
    assert report.time_weighted_return_pct == pytest.approx(report.return_pct)


def test_excess_sharpe_subtracts_the_cash_rate():
    daily = 0.05 / 252
    flat = [1 + daily] * 6
    assert excess_sharpe(flat, 0.05) == pytest.approx(0.0, abs=1e-9)
    ahead = [1 + daily + 0.01, 1 + daily - 0.005, 1 + daily + 0.002, 1 + daily + 0.004]
    assert excess_sharpe(ahead, 0.05) > 0
    assert excess_sharpe([1.01], 0.05) is None


def test_hold_compounds_the_quoted_rate(store):
    engine = make_engine(store, interval="daily", start="2025-01-28", end="2025-02-04", symbols=["AAPL"], cash_annual_rate=0.05, run_id="compound")
    report = run_to_end(engine)
    expected = ((1 + 0.05 / 252) ** report.steps - 1) * 100
    assert report.time_weighted_return_pct == pytest.approx(expected)
    assert report.net_profit == pytest.approx(report.interest_earned)
    assert report.interest_earned == pytest.approx(report.final_equity - 10_000)


def test_net_profit_excludes_deposits_and_drawdown_is_deposit_adjusted(store):
    engine = make_engine(store, daily_deposit=200, cash_annual_rate=0.05, run_id="both")
    report = run_to_end(engine)
    assert report.total_deposits > 0 and report.interest_earned > 0
    assert report.net_profit == pytest.approx(report.final_equity - 10_000 - report.total_deposits)
    assert report.net_profit == pytest.approx(report.interest_earned)
    equity = [value for _, _, value in engine.equity_points]
    adjusted = [value - deposited for value, deposited in zip(equity, engine._deposits_at_point)]
    assert report.max_drawdown_pct == pytest.approx(max_drawdown_pct(adjusted))
    # Deposits are external. The quoted rate on a rising cash balance is the whole time-weighted return.
    expected = ((1 + 0.05 / 252) ** len(report.daily_cash) - 1) * 100
    assert report.time_weighted_return_pct == pytest.approx(expected)
    assert report.time_weighted_return_pct != pytest.approx(report.return_pct)
    # Cash earns exactly the rate that Sharpe subtracts, so the ratio is zero.
    assert report.sharpe == pytest.approx(0.0, abs=1e-9)
    assert report.sharpe_periods_per_year == 252


class _Marks:
    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    def marks(self) -> dict:
        return {symbol: {"close": price} for symbol, price in self._prices.items()}


def _request(step: int, cash: float, deposit: float) -> dict:
    return {
        "run_id": "r",
        "request_id": f"decision_{step:05d}",
        "mode": "V1",
        "window_start": "2025-01-28T14:30:00Z",
        "window_end": "2025-01-29T14:30:00Z",
        "step": step,
        "account": {"cash": cash, "deposit_today": deposit},
        "data": {"symbols": ["AAA", "BBB"]},
        "limits": {"max_position_pct_equity": 0, "slippage_bps": 0},
    }


def test_buy_and_hold_invests_starting_cash_and_each_deposit():
    agent = BuyAndHoldAgent()
    marks = _Marks({"AAA": 10.0, "BBB": 10.0})
    first = agent.decide(_request(0, 10_000, 0), marks)
    assert [order["quantity"] for order in first["orders"]] == [490, 490]
    assert [order["order_id"] for order in first["orders"]] == ["bh_00", "bh_01"]

    later = agent.decide(_request(4, 5_000, 200), marks)
    # 200 * 0.98 / 2 = 98, and each share is 10, so 9 shares of each name.
    assert [order["quantity"] for order in later["orders"]] == [9, 9]
    assert later["orders"][0]["order_id"] == "bh_00004_00"

    assert agent.decide(_request(5, 5_000, 0), marks)["orders"] == []
    assert CashOnlyAgent().decide(_request(0, 10_000, 200), marks)["orders"] == []


def _run(store, agent, run_id: str) -> dict:
    service = SimulatorService(store, persist=False)
    cfg = {
        "run_id": run_id,
        "interval": "daily",
        "mode": "V1",
        "start": "2025-01-28",
        "end": "2025-02-04",
        "symbols": ["AAPL", "MSFT"],
        "initial_cash": 10_000,
        "daily_deposit": 200,
        "cash_annual_rate": 0.05,
        "history_lookback_days": 5,
        "news_lookback_days": 10,
        "execution": {"max_position_pct_equity": 0, "slippage_bps": 1.0},
    }
    first = service.reset(cfg)
    feed = LocalFeedReader(service, first["run_id"])
    return run_agent(agent, lambda _cfg: first, service.submit_action, feed, cfg, verbose=False)


def test_benchmarks_share_deposits_and_interest(store):
    held = _run(store, CashOnlyAgent(), "cash-bench")
    bought = _run(store, BuyAndHoldAgent(), "bh-bench")
    assert held["fills"] == 0
    assert bought["fills"] > 0
    assert held["total_deposits"] == pytest.approx(bought["total_deposits"])
    assert held["total_deposits"] == pytest.approx(200 * held["steps"])
    assert held["net_profit"] == pytest.approx(held["final_equity"] - 10_000 - held["total_deposits"])
    assert bought["net_profit"] == pytest.approx(bought["final_equity"] - 10_000 - bought["total_deposits"])
    assert held["net_profit"] == pytest.approx(held["interest_earned"])
    expected = ((1 + 0.05 / 252) ** held["steps"] - 1) * 100
    assert held["time_weighted_return_pct"] == pytest.approx(expected)
    # Deployed cash earns less interest than cash that never trades.
    assert bought["interest_earned"] < held["interest_earned"]
    assert bought["final_positions"]


def test_prompt_tells_the_model_it_can_keep_cash():
    text = (ROOT / "meterless" / "trader.py").read_text(encoding="utf-8")
    assert "maximize profit" in text
    assert "You can buy stocks or keep cash" in text
    assert "cash_annual_rate" in text
    assert "You cannot create cash" in text
    assert "Cash by itself earns nothing" not in text
