from __future__ import annotations

import json

import httpx
import pytest

from meterless.action import fit_orders, structure_action
from meterless.query import QueryRequest, SqliteSource, ZetarisSource, open_analysis_source
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
    trader = NemotronTrader(Runtime(), source=source)
    action = trader.decide(request)
    assert action["orders"] == []
    assert "31.3" not in action["reason"]
    assert action["reason"].startswith("decision follows SQL results")
    assert trader.queries[0].as_of == "2024-01-03T14:30:00Z"
    assert trader.queries[0].rows[0]["close"] == 185.64


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


def test_open_rejects_an_unknown_backend(tmp_path):
    with pytest.raises(ValueError):
        open_analysis_source("mssql", snapshot_dir=tmp_path)
