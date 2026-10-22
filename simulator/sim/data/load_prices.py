"""Fetch real OHLCV bars from the public Yahoo Finance chart endpoint.

Hourly bars (interval ``1h``) are only served for roughly the trailing two to three
years, so the snapshot must be frozen while early-2024 bars are still available.
Daily bars carry ``adj_close``; hourly bars do not (Yahoo serves them unadjusted).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import pandas as pd

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "Mozilla/5.0 (ai-trader-sim research collector)"

BAR_COLUMNS = ["symbol", "interval", "ts", "open", "high", "low", "close", "volume", "adj_close"]


def _epoch(date_str: str) -> int:
    return int(datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc).timestamp())


def fetch_bars(symbol: str, interval: str, start: str, end_exclusive: str, client: httpx.Client | None = None) -> pd.DataFrame:
    """Return bars for ``symbol`` at Yahoo interval code (``1h`` or ``1d``) in [start, end)."""
    own_client = client is None
    client = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    try:
        params: dict[str, str | int] = {"interval": interval, "includePrePost": "false", "events": "div,splits"}
        if interval == "1h":
            # Yahoo rejects explicit period1 values older than 730 days for intraday
            # intervals, but the range form returns everything it still holds
            # (observed: back to late 2023). We filter to [start, end) locally.
            params["range"] = "730d"
        else:
            params["period1"] = _epoch(start)
            params["period2"] = _epoch(end_exclusive)
        resp = client.get(YAHOO_CHART.format(symbol=symbol), params=params)
        resp.raise_for_status()
        payload = resp.json()
    finally:
        if own_client:
            client.close()

    chart = payload.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(f"Yahoo error for {symbol} {interval}: {chart['error']}")
    result = chart["result"][0]
    timestamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    adj = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose")

    frame = pd.DataFrame(
        {
            "ts": pd.to_datetime(timestamps, unit="s", utc=True),
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "close": quote.get("close"),
            "volume": quote.get("volume"),
            "adj_close": adj if adj is not None else [None] * len(timestamps),
        }
    )
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end_exclusive, tz="UTC")
    frame = frame[(frame["ts"] >= lo) & (frame["ts"] < hi)]
    frame["symbol"] = symbol
    frame["interval"] = "hourly" if interval == "1h" else "daily"
    frame["volume"] = frame["volume"].fillna(0).astype("int64")
    frame = frame[BAR_COLUMNS].sort_values("ts").drop_duplicates(subset=["ts"], keep="last")
    return frame.reset_index(drop=True)


def fetch_universe_bars(symbols: list[str], start: str, end_inclusive: str, pause_s: float = 0.6) -> dict[str, pd.DataFrame]:
    end_exclusive = (pd.Timestamp(end_inclusive) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    out: dict[str, list[pd.DataFrame]] = {"hourly": [], "daily": []}
    with httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        for symbol in symbols:
            for name, code in (("hourly", "1h"), ("daily", "1d")):
                frame = fetch_bars(symbol, code, start, end_exclusive, client=client)
                out[name].append(frame)
                print(f"  {symbol:6} {name:6} {len(frame):6} bars  {frame.ts.min()} -> {frame.ts.max()}")
                time.sleep(pause_s)
    return {k: pd.concat(v, ignore_index=True) if v else pd.DataFrame(columns=BAR_COLUMNS) for k, v in out.items()}
