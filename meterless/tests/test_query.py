from __future__ import annotations

import json

import httpx
import pytest

from meterless.action import fit_orders, structure_action
from meterless.query import JdbcZetarisSource, QueryRequest, SqliteSource, ZetarisSource, open_analysis_source
from meterless.recall import fetch_brief
from meterless.trader import NemotronTrader


BARS = [
    {"symbol": "AAPL", "interval": "daily", "ts": "2024-01-02T14:30:00Z", "open": 187.15, "high": 188.44, "low": 183.89, "close": 185.64, "volume": 100},
    {"symbol": "AAPL", "interval": "daily", "ts": "2024-01-03T14:30:00Z", "open": 184.22, "high": 185.88, "low": 183.43, "close": 184.25, "volume": 80},
]
FILINGS = [
    {"symbol": "AAPL", "form": "8-K", "title": "Results of Operations", "published_at": "2024-01-02T21:29:17Z", "available_at": "2024-01-02T21:29:17Z"},
    {"symbol": "AAPL", "form": "8-K", "title": "Later filing", "published_at": "2024-01-04T15:00:00Z", "available_at": "2024-01-04T15:00:00Z"},
]


def _request(sql: str, as_of: str) -> QueryRequest:
    return QueryRequest("run", "decision_00000", "decision_00000-q1", "month return", sql, as_of)


def test_cutoff_hides_later_bars_and_filings(tmp_path):
    source = SqliteSource.build(tmp_path / "market.sqlite", BARS, FILINGS)
    early = source.execute(_request("SELECT ts FROM bars ORDER BY ts", "2024-01-03T14:30:00Z"))
    assert early.error == ""
    assert [row["ts"] for row in early.rows] == ["2024-01-02T14:30:00Z"]
    before_filing = source.execute(_request("SELECT title FROM filings", "2024-01-02T14:30:00Z"))
    assert before_filing.rows == []
    released = source.execute(_request("SELECT title FROM filings ORDER BY published_at", "2024-01-03T14:30:00Z"))
    assert [row["title"] for row in released.rows] == ["Results of Operations"]
    later = source.execute(_request("SELECT title FROM filings ORDER BY published_at", "2024-01-04T15:00:00Z"))
    assert [row["title"] for row in later.rows] == ["Results of Operations", "Later filing"]
    both = source.execute(_request("SELECT ts FROM bars WHERE ts < '2099-01-01T00:00:00Z' ORDER BY ts", "2024-01-03T14:30:00Z"))
    assert [row["ts"] for row in both.rows] == ["2024-01-02T14:30:00Z"]


def test_writes_and_base_tables_are_refused(tmp_path):
    source = SqliteSource.build(tmp_path / "market.sqlite", BARS, FILINGS)
    denied = source.execute(_request("INSERT INTO bars VALUES ('AAPL','daily','2024-01-02T14:30:00Z',1,1,1,1,1)", "2024-01-04T00:00:00Z"))
    assert denied.error
    hidden = source.execute(_request("SELECT ts FROM price_history", "2024-01-04T00:00:00Z"))
    assert hidden.error
    assert hidden.rows == []


def test_zetaris_prepares_the_same_relations_and_the_server_cutoff():
    source = ZetarisSource("http://zetaris.test/sql", token="secret", data_version="zet-1")
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        sent["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"columns": ["symbol", "close"], "rows": [{"symbol": "AAPL", "close": 185.64}]})

    source._client = httpx.Client(transport=httpx.MockTransport(handler))
    result = source.execute(_request("SELECT symbol, close FROM bars", "2024-01-03T14:30:00+00:00"))
    assert result.backend == "zetaris"
    assert result.data_version == "zet-1"
    assert result.rows == [{"symbol": "AAPL", "close": 185.64}]
    assert sent["auth"] == "Bearer secret"
    assert "FROM released_bars" in sent["body"]["sql"]
    assert "bar_end <= '2024-01-03T14:30:00Z'" in sent["body"]["sql"]
    assert "FROM bars" in sent["body"]["sql"]
    assert sent["body"]["as_of"] == "2024-01-03T14:30:00Z"
    assert source.schema_text == SqliteSource.schema_text
    with pytest.raises(ValueError):
        ZetarisSource("http://zetaris.test/sql", bars_view="bars")


def test_trader_binds_the_simulator_cutoff_and_drops_an_invented_figure(tmp_path):
    source = SqliteSource.build(tmp_path / "market.sqlite", BARS, FILINGS)
    replies = iter([
        json.dumps({"goal": "latest completed close", "sql": "SELECT close FROM bars ORDER BY ts DESC LIMIT 1"}),
        json.dumps({
            "schema_version": "1.0", "run_id": "run", "request_id": "decision_00000", "mode": "V1",
            "window_start": "2024-01-03T14:30:00Z", "window_end": "2024-01-04T14:30:00Z",
            "orders": [], "reason": "AAPL fell 31.3 percent",
        }),
    ])

    class Runtime:
        def complete(self, _content: str, max_tokens: int | None = None) -> str:
            return next(replies)

    request = {
        "run_id": "run", "request_id": "decision_00000", "mode": "V1", "as_of": "2024-01-03T14:30:00Z",
        "window_start": "2024-01-03T14:30:00Z", "window_end": "2024-01-04T14:30:00Z",
    }
    trader = NemotronTrader(Runtime(), source=source, log_dir=tmp_path)
    action = trader.decide(request)
    assert action["orders"] == []
    # The original reason stays in the envelope. The unsupported-number flag is logged beside it.
    assert "31.3" in action["reason"]
    assert trader.repairs[-1]["reason_unsupported"] is True
    assert trader.repairs[-1]["reason"] == action["reason"]
    assert trader.queries[0].as_of == "2024-01-03T14:30:00Z"
    assert trader.queries[0].rows[0]["close"] == 185.64
    logged = json.loads((tmp_path / "order_repairs.jsonl").read_text().splitlines()[-1])
    assert logged["reason_unsupported"] is True
    calls = [json.loads(line) for line in (tmp_path / "model_calls.jsonl").read_text().splitlines()]
    assert len(calls) == 2
    assert calls[0]["request_id"] == "decision_00000"


def test_structure_action_keeps_orders_and_fills_ids_from_the_request():
    request = {
        "run_id": "run_real", "request_id": "decision_00007", "mode": "V1",
        "window_start": "2024-01-16T14:30:00Z", "window_end": "2024-01-17T14:30:00Z",
    }
    action = structure_action({
        "run_id": "run_other", "as_of": "nope", "limit_price": None,
        "orders": [{"symbol": "aapl", "side": "buy", "quantity": "10", "order_type": "MARKET", "limit_price": 1}],
        "reason": "rising close",
    }, request)
    assert action["run_id"] == "run_real"
    assert action["request_id"] == "decision_00007"
    assert action["window_start"] == "2024-01-16T14:30:00Z"
    assert "as_of" not in action
    order = action["orders"][0]
    assert order["symbol"] == "AAPL"
    assert order["side"] == "BUY"
    assert order["quantity"] == 10
    assert order["execute_at"] == "2024-01-16T14:30:00Z"
    assert order["limit_price"] is None


def test_fit_orders_shrinks_a_buy_to_the_position_cap():
    request = {
        "account": {"cash": 10000, "equity": 10000, "positions": []},
        "limits": {"max_position_pct_equity": 0.25, "slippage_bps": 1},
    }
    action = fit_orders({
        "orders": [{"symbol": "AAPL", "side": "BUY", "quantity": 500, "order_type": "MARKET", "limit_price": None}],
        "reason": "rising",
    }, request, {"AAPL": 190.0})
    assert action["orders"][0]["quantity"] == 12


def test_fit_orders_uses_cash_when_the_position_cap_is_off():
    request = {
        "account": {"cash": 10000, "equity": 10000, "positions": []},
        "limits": {"max_position_pct_equity": 0, "slippage_bps": 1},
    }
    action = fit_orders({
        "orders": [{"symbol": "AAPL", "side": "BUY", "quantity": 500, "order_type": "MARKET", "limit_price": None}],
        "reason": "rising",
    }, request, {"AAPL": 190.0})
    assert action["orders"][0]["quantity"] == 51


def test_brief_states_the_change_and_hides_a_later_close(tmp_path):
    days = [(2, 100), (3, 102), (4, 104), (5, 106), (8, 108), (9, 110), (10, 112), (11, 120), (12, 999)]
    bars = [
        {"symbol": "AAPL", "interval": "daily", "ts": f"2024-01-{day:02d}T14:30:00Z", "open": close, "high": close, "low": close, "close": close, "volume": 1}
        for day, close in days
    ]
    source = SqliteSource.build(tmp_path / "market.sqlite", bars, [])
    text, found = fetch_brief(source, {
        "run_id": "run", "request_id": "decision_00000", "as_of": "2024-01-12T14:30:00Z",
        "data": {"symbols": ["AAPL"]},
    })
    assert found[0].error == ""
    assert "999" not in text
    assert "5 sessions earlier 2024-01-04 104.00 (+15.38%)" in text
    assert "7 sessions earlier 2024-01-02 100.00 (+20.00%)" in text


def test_jdbc_cte_maps_the_lake_and_binds_the_cutoff():
    from datetime import datetime, timezone

    source = JdbcZetarisSource(connect=False)
    sent = source.prepare("SELECT symbol, close FROM bars WHERE interval = 'daily'", "2024-01-03T14:30:00+00:00")
    cutoff = int(datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc).timestamp())
    assert "FROM aitrader_lake.feed.feed_prices_daily" in sent
    assert "FROM aitrader_lake.feed.feed_prices_hourly" in sent
    assert "FROM aitrader_lake.feed.feed_news" in sent
    assert f"_released_epoch <= {cutoff}" in sent
    assert "FROM bars" in sent
    assert "SELECT symbol, close FROM bars" in sent
    with pytest.raises(ValueError, match="bars and filings"):
        source.prepare("SELECT close FROM feed_prices_daily", "2024-01-03T14:30:00Z")


def test_default_backend_is_the_jdbc_lake(monkeypatch):
    monkeypatch.delenv("ANALYSIS_BACKEND", raising=False)
    monkeypatch.setattr(JdbcZetarisSource, "from_env", classmethod(lambda cls, expected_version=None: "jdbc-lake"))
    assert open_analysis_source() == "jdbc-lake"


def test_open_rejects_an_unknown_backend(tmp_path):
    with pytest.raises(ValueError):
        open_analysis_source("mssql", snapshot_dir=tmp_path)
