"""Shape the frozen snapshot into the released-feed tables published to Zetaris.

The simulator owns released-feed writes. Instead of inserting rows step by step, the whole
snapshot is loaded once with each row's release time in a hidden `_released_epoch` column;
the agent's query tool exposes a row only when `_released_epoch <= as_of`. That gives the
same visibility, at every decision boundary, as the run's own feed:

  * prices: a bar is released at `bar_end`, computed exactly as MarketTimeline does.
  * news:   NewsQueue releases a record at the first boundary t with
            `published_at <= t` and `available_at <= t`, so it is visible at as_of iff
            `max(published_at, available_at) <= as_of`.

`_scope_epoch` carries the column a run's lookback window applies to (bar `ts`, news
`available_at`). The agent-facing columns must match FEED_TABLES in agent/config.py; this
module cannot import it, so tests/test_publish_columns.py at the repo root checks them.

No JDBC here: rows are plain dicts. scripts/publish_zetaris.py does the writing.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from sim.store import MarketTimeline, SnapshotStore

PRICE_COLUMNS = ["symbol", "ts", "open", "high", "low", "close", "volume", "bar_end", "released_at"]
NEWS_COLUMNS = ["event_id", "symbol", "provider", "form", "title", "summary", "source_url",
                "published_at", "available_at", "released_at"]
HIDDEN_COLUMNS = ["_released_epoch", "_scope_epoch", "data_version"]

# Spark SQL types for the published tables. Timestamps stay ISO-8601 strings so the
# Zetaris rows are byte-identical to the local SQLite feed the runner builds.
COLUMN_TYPES = {
    "open": "DOUBLE", "high": "DOUBLE", "low": "DOUBLE", "close": "DOUBLE",
    "volume": "BIGINT", "_released_epoch": "BIGINT", "_scope_epoch": "BIGINT",
}


def _epoch(ts: pd.Timestamp) -> int:
    return int(ts.timestamp())


def _clean(value: Any) -> Any:
    """NaN/NaT -> None, numpy scalars -> Python."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        return value.item()
    return value


def news_release_time(published_at: pd.Timestamp, available_at: pd.Timestamp) -> pd.Timestamp:
    return max(published_at, available_at)


def price_rows(store: SnapshotStore, interval: str) -> list[dict]:
    symbols = store.symbols(interval)
    frame = store.bars(interval, symbols, pd.Timestamp.min.tz_localize("UTC"), pd.Timestamp.max.tz_localize("UTC"))
    timeline = MarketTimeline(frame, interval)
    rows = []
    for symbol, series in timeline.series.items():
        for i, ts_ns in enumerate(series.ts):
            ts = pd.Timestamp(int(ts_ns), unit="ns", tz="UTC")
            bar_end = pd.Timestamp(int(series.bar_end[i]), unit="ns", tz="UTC")
            rows.append({
                "symbol": symbol,
                "ts": ts.isoformat(),
                "open": float(series.open[i]),
                "high": float(series.high[i]),
                "low": float(series.low[i]),
                "close": float(series.close[i]),
                "volume": int(series.volume[i]),
                "bar_end": bar_end.isoformat(),
                "released_at": bar_end.isoformat(),
                "_released_epoch": _epoch(bar_end),
                "_scope_epoch": _epoch(ts),
                "data_version": store.data_version,
            })
    return rows


def news_rows(store: SnapshotStore) -> list[dict]:
    symbols = sorted(set(store.symbols("hourly")) | set(store.symbols("daily")))
    frame = store.news(symbols, pd.Timestamp.max.tz_localize("UTC"))
    rows = []
    for rec in frame.to_dict(orient="records"):
        released = news_release_time(rec["published_at"], rec["available_at"])
        row = {c: _clean(rec.get(c)) for c in NEWS_COLUMNS if c not in ("published_at", "available_at", "released_at")}
        row.update({
            "published_at": rec["published_at"].isoformat(),
            "available_at": rec["available_at"].isoformat(),
            "released_at": released.isoformat(),
            "_released_epoch": _epoch(released),
            "_scope_epoch": _epoch(rec["available_at"]),
            "data_version": store.data_version,
        })
        rows.append(row)
    return rows


def table_ddl(table: str, columns: list[str], using: str | None = "iceberg") -> str:
    cols = ", ".join(f"{c} {COLUMN_TYPES.get(c, 'STRING')}" for c in columns + HIDDEN_COLUMNS)
    suffix = f" USING {using}" if using else ""
    return f"CREATE TABLE IF NOT EXISTS {table} ({cols}){suffix}"


def manifest_ddl(table: str, using: str | None = "iceberg") -> str:
    suffix = f" USING {using}" if using else ""
    return (f"CREATE TABLE IF NOT EXISTS {table} (data_version STRING, loaded_at STRING, "
            f"prices_hourly_rows BIGINT, prices_daily_rows BIGINT, news_rows BIGINT){suffix}")
