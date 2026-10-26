"""Explore cannot see the test window, accounts stay isolated, and rules repeat."""
from __future__ import annotations

import pytest

import meterless.experiment as experiment
from agent.scripted import AccumulatingBuyAndHold, make_order
from meterless.query import QueryRequest, SqliteSource
from meterless.strategy import Signal, Selection, StrategySpec, feature_sql, load_features
from sim.service import SimulatorService
from sim.store import SnapshotStore


FIXTURES = experiment.SIM / "tests" / "fixtures"


def _bars() -> list[dict]:
    rows = []
    for offset, close in enumerate(range(100, 130)):
        day = f"2024-01-{offset + 1:02d}"
        rows.append({
            "symbol": "AAPL", "interval": "daily", "ts": f"{day}T00:00:00Z",
            "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000,
        })
    rows.append({
        "symbol": "AAPL", "interval": "daily", "ts": "2024-02-01T00:00:00Z",
        "open": 9999, "high": 9999, "low": 9999, "close": 9999, "volume": 1000,
    })
    return rows


def _filings() -> list[dict]:
    return [
        {"symbol": "AAPL", "form": "10-Q", "title": "released", "published_at": "2024-01-15T12:00:00Z", "available_at": "2024-01-15T12:00:00Z"},
        {"symbol": "AAPL", "form": "8-K", "title": "dated ahead", "published_at": "2024-02-15T12:00:00Z", "available_at": "2024-01-20T12:00:00Z"},
        {"symbol": "AAPL", "form": "10-K", "title": "not released", "published_at": "2024-03-01T12:00:00Z", "available_at": "2024-03-01T12:00:00Z"},
    ]


def test_feature_view_stops_at_as_of(tmp_path):
    source = SqliteSource.build(tmp_path / "lake.sqlite", _bars(), _filings())
    as_of = "2024-02-01T14:30:00Z"
    sql = feature_sql(["AAPL"], as_of)
    assert "feed_prices" not in sql and "_released" not in sql
    rows = load_features(source, as_of, ["AAPL"])
    assert load_features.last_error == ""
    assert rows and rows[0]["symbol"] == "AAPL"
    assert rows[0]["close"] != 9999
    assert rows[0]["close"] == 129
    assert rows[0]["ret_5"] == pytest.approx(129 / 124 - 1)
    assert rows[0]["last_filing"] == "2024-01-15"
    assert rows[0]["next_filing"] == ""
    assert rows[0]["days_since_filing"] == 17
    released = source.execute(QueryRequest(
        run_id="t", request_id="t", query_id="filings", goal="cutoff",
        sql="SELECT title FROM filings",
        as_of=as_of,
    ), row_limit=20)
    titles = [row["title"] for row in released.rows]
    assert titles == ["released"]
    direct = source.execute(QueryRequest(
        run_id="t", request_id="t", query_id="t", goal="cutoff",
        sql="SELECT close FROM bars WHERE interval = 'daily' AND symbol = 'AAPL' ORDER BY ts",
        as_of=as_of,
    ), row_limit=100)
    assert 9999 not in [row["close"] for row in direct.rows]


def test_test_notebook_has_no_test_window():
    explore = "Highest explore profit was 12.50 on 2024-01-02 to 2024-01-31. sample_days=21. The sample is uncertain."
    assert experiment.notes_for_test(explore) == explore
    assert experiment.leaks_test_window(explore, "2024-02-01") is False
    assert experiment.leaks_test_window(explore + " 2024-02-01", "2024-02-01") is True
    sample = {"explore_start": "2024-01-01", "explore_end": "2024-01-31", "test_start": "2024-02-01", "n_days": 21, "n_specs": 2}
    rows = [
        {"arm": "explore", "spec_id": "root-s01", "window_end": "2024-01-31", "net_profit": 12.5, "twr": 1.2, "max_drawdown": 0.4, "turnover": 0.1, "excess": 0.2},
        {"arm": "explore", "spec_id": "root-s02", "window_end": "2024-01-31", "net_profit": -3.0, "twr": -0.4, "max_drawdown": 1.1, "turnover": 0.2, "excess": -1.0},
    ]
    experiment.assert_rows_before(rows, "2024-02-01")
    with pytest.raises(ValueError):
        experiment.assert_rows_before([{"window_end": "2024-02-01"}], "2024-02-01")
    notes = experiment.mechanical_findings(rows, sample)
    assert experiment.leaks_test_window(notes, "2024-02-01") is False
    leaked = '{"findings":[{"text":"February 2024-02-03 worked","evidence":"12.5"}]}'
    rendered = experiment.render_findings(leaked, "12.5 21 2", sample, rows)
    assert "2024-02-03" not in rendered
    assert experiment.leaks_test_window(rendered, "2024-02-01") is False
    with pytest.raises(ValueError):
        experiment.assert_allowed_years("2025-01-01", "2025-01-31", "")
    experiment.assert_allowed_years("2025-01-01", "2025-01-31", "validation")


def test_accounts_are_isolated():
    import pandas as pd

    store = SnapshotStore(
        {name: pd.read_parquet(FIXTURES / f"bars_{name}.parquet") for name in ("hourly", "daily")},
        pd.read_parquet(FIXTURES / "news.parquet"),
        data_version="fixture",
    )
    left = SimulatorService(store, persist=False)
    right = SimulatorService(store, persist=False)
    base = {
        "interval": "hourly", "mode": "V1", "start": "2025-01-28", "end": "2025-01-28",
        "symbols": ["AAPL"], "initial_cash": 10000, "daily_deposit": 200, "cash_annual_rate": 0.05,
        "execution": {"max_position_pct_equity": 0},
    }
    request_left = left.reset({**base, "run_id": "iso-left", "label": "left"})
    request_right = right.reset({**base, "run_id": "iso-right", "label": "right"})
    assert request_left["run_id"] != request_right["run_id"]
    cash_right = right.state("iso-right")["cash"]
    order = make_order(request_left, "buy1", "AAPL", "BUY", 1)
    payload = {
        "schema_version": "1.0", "run_id": request_left["run_id"], "request_id": request_left["request_id"],
        "mode": "V1", "window_start": request_left["window_start"], "window_end": request_left["window_end"],
        "orders": [order], "reason": "isolation",
    }
    left.submit_action("iso-left", payload)
    assert left.state("iso-left")["cash"] < cash_right
    assert right.state("iso-right")["cash"] == cash_right
    assert right.state("iso-right")["positions"] == []
    assert left.engines["iso-left"] is not right.engines["iso-right"]


def _features(_as_of: str) -> list[dict]:
    return [
        {"symbol": "AAPL", "ret_5": 0.04, "ret_21": 0.08, "ret_63": 0.1, "vol_21": 0.01, "ma_gap_21": 0.02, "ma_gap_63": 0.03, "dd_63": 0.01, "rs_spy": 0.02, "days_since_filing": 4},
        {"symbol": "MSFT", "ret_5": -0.02, "ret_21": 0.01, "ret_63": 0.02, "vol_21": 0.03, "ma_gap_21": -0.01, "ma_gap_63": 0.0, "dd_63": 0.04, "rs_spy": -0.01, "days_since_filing": 30},
    ]


def test_rule_runner_is_deterministic():
    import pandas as pd

    store = SnapshotStore(
        {name: pd.read_parquet(FIXTURES / f"bars_{name}.parquet") for name in ("hourly", "daily")},
        pd.read_parquet(FIXTURES / "news.parquet"),
        data_version="fixture",
    )
    spec = StrategySpec(
        name="mom-5",
        universe=["AAPL", "MSFT"],
        signal=Signal(kind="momentum", lookback=5),
        selection=Selection(mode="top_n", top_n=1),
        target_weight=0.30,
        cash_floor=0.10,
        rebalance_every=1,
        min_weight_change=0.01,
        drawdown_cut=0.50,
    ).model_dump()
    orders = []
    profits = []
    for run_id in ("rule-a", "rule-b"):
        service = SimulatorService(store, persist=False)
        cfg = experiment.run_config("2025-01-28", "2025-02-04", ["AAPL", "MSFT"], "rule", run_id)
        cfg["interval"] = "daily"
        agent = experiment.RuleRunner(spec, _features)
        seen = []

        class Recording:
            def decide(self, request, feed):
                payload = agent.decide(request, feed)
                seen.append([(item["symbol"], item["side"], item["quantity"]) for item in payload["orders"]])
                return payload

        first = service.reset(cfg)
        feed = experiment.LocalFeedReader(service, first["run_id"])
        report = experiment.run_agent(Recording(), lambda _cfg: first, service.submit_action, feed, cfg, verbose=False)
        orders.append(seen)
        profits.append(report["net_profit"])
    assert orders[0] == orders[1]
    assert orders[0]
    assert profits[0] == profits[1]


class _Marks:
    def __init__(self, marks):
        self._marks = marks

    def marks(self):
        return self._marks


def _request(step: int, cash: float, deposit: float) -> dict:
    return {
        "run_id": "bha",
        "request_id": f"r{step}",
        "mode": "V1",
        "step": step,
        "window_start": "2024-01-02T14:30:00Z",
        "window_end": "2024-01-03T14:30:00Z",
        "account": {"cash": cash, "deposit_today": deposit, "equity": cash},
        "data": {"symbols": ["AAPL", "MSFT", "SPY"]},
        "limits": {"max_position_pct_equity": 0, "slippage_bps": 1},
    }


def test_accumulating_buy_and_hold_waits_for_a_whole_share():
    agent = AccumulatingBuyAndHold()
    pricey = {"AAPL": {"close": 4000}, "MSFT": {"close": 4000}, "SPY": {"close": 4000}}
    first = agent.decide(_request(0, 300, 0), _Marks(pricey))
    assert first["orders"] == []
    purse = agent.purse
    second = agent.decide(_request(1, 500, 200), _Marks(pricey))
    assert second["orders"] == []
    assert agent.purse == pytest.approx(purse + 200)
    later = agent.decide(_request(2, 5000, 4500), _Marks({"AAPL": {"close": 100}, "MSFT": {"close": 4000}, "SPY": {"close": 4000}}))
    assert any(order["side"] == "BUY" and order["symbol"] == "AAPL" and order["quantity"] >= 1 for order in later["orders"])
    assert all(order["order_id"].startswith("bha_") for order in later["orders"])
