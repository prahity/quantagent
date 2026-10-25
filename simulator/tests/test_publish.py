"""The one-time Zetaris load must expose, at every decision boundary, exactly what the run's
own feed releases: same news ids, same completed bars."""
import pandas as pd
import pytest

from sim.publish import news_rows, price_rows, table_ddl, PRICE_COLUMNS
from tests.conftest import hold, make_engine


def _visible(rows, as_of_epoch, from_epoch, symbols):
    return [r for r in rows
            if r["_released_epoch"] <= as_of_epoch and r["_scope_epoch"] >= from_epoch and r["symbol"] in symbols]


@pytest.mark.parametrize("interval", ["hourly", "daily"])
def test_published_rows_match_the_run_feed_at_every_boundary(store, interval):
    engine = make_engine(store, run_id=f"pub_{interval}", interval=interval, symbols=["AAPL", "MSFT", "SPY"],
                         start="2025-01-28", end="2025-02-06")
    cfg = engine.config
    start = pd.Timestamp(cfg.start, tz="UTC")
    prices_from = int((start - pd.Timedelta(days=cfg.history_lookback_days)).timestamp())
    news_from = int((start - pd.Timedelta(days=cfg.news_lookback_days)).timestamp())
    prices, news = price_rows(store, interval), news_rows(store)

    request, steps = engine.current_request, 0
    while request is not None:
        as_of = request.as_of
        epoch = int(as_of.timestamp())

        want_news = set(engine.feed.news()["event_id"])
        got_news = {r["event_id"] for r in _visible(news, epoch, news_from, engine.symbols)}
        assert got_news == want_news, as_of

        for symbol in engine.symbols:
            bars = engine.feed.prices(symbol)
            want = [(t.isoformat(), c, e.isoformat()) for t, c, e in zip(bars.ts, bars.close, bars.bar_end)]
            got = [(r["ts"], r["close"], r["bar_end"]) for r in _visible(prices, epoch, prices_from, {symbol})]
            assert got == want, (as_of, symbol)

        _, request, _ = engine.submit_action(hold(request))
        steps += 1
    assert steps > 5


def test_rows_carry_hidden_columns_and_version(store):
    row = price_rows(store, "hourly")[0]
    assert set(PRICE_COLUMNS) <= set(row)
    assert row["data_version"] == store.data_version
    assert row["released_at"] == row["bar_end"]
    for rec in news_rows(store):
        assert rec["released_at"] >= rec["published_at"] and rec["released_at"] >= rec["available_at"]


def test_ddl_types():
    ddl = table_ddl("lake.feed.feed_prices_hourly", PRICE_COLUMNS)
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS lake.feed.feed_prices_hourly (")
    assert "close DOUBLE" in ddl and "volume BIGINT" in ddl and "_released_epoch BIGINT" in ddl
    assert ddl.endswith("USING iceberg")
