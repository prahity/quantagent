"""Frozen snapshot access and the per-run market timeline.

The private archive (including future rows) lives here. Nothing in this module is
exposed to an agent directly; ``feed.Feed`` applies the as_of cutoff.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from sim.config import MANIFEST_PATH, SNAPSHOT_DIR
from sim.data.manifest import read_manifest

NOMINAL_SECONDS = {"hourly": 3600, "daily": 86400}


class SnapshotError(RuntimeError):
    pass


def _ts(t) -> pd.Timestamp:
    """Accept pandas Timestamps or stdlib datetimes; always return tz-aware UTC ns."""
    ts = pd.Timestamp(t)
    ts = ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
    return ts.as_unit("ns")


def _ns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Parquet round-trips at microsecond resolution; all internal arithmetic is in ns UTC."""
    frame = frame.copy()
    for col in columns:
        if col in frame.columns:
            frame[col] = pd.to_datetime(frame[col], utc=True).dt.as_unit("ns")
    return frame


class SnapshotStore:
    def __init__(self, bars: dict[str, pd.DataFrame], news: pd.DataFrame, data_version: str, manifest: dict | None = None):
        self._bars = {k: _ns(v, ["ts"]).sort_values(["symbol", "ts"]).reset_index(drop=True) for k, v in bars.items()}
        news = _ns(news, ["published_at", "available_at"]) if not news.empty else news
        self._news = news.sort_values(["available_at", "event_id"]).reset_index(drop=True) if not news.empty else news
        self.data_version = data_version
        self.manifest = manifest or {}

    @classmethod
    def load(cls, snapshot_dir: Path = SNAPSHOT_DIR, manifest_path: Path = MANIFEST_PATH) -> "SnapshotStore":
        bars = {}
        for interval in ("hourly", "daily"):
            path = snapshot_dir / f"bars_{interval}.parquet"
            if not path.exists():
                raise SnapshotError(f"missing {path}. Run: python scripts/fetch_data.py")
            bars[interval] = pd.read_parquet(path)
        news_path = snapshot_dir / "news.parquet"
        news = pd.read_parquet(news_path) if news_path.exists() else pd.DataFrame()
        manifest = read_manifest(manifest_path) or {}
        return cls(bars, news, manifest.get("data_version", "unpinned"), manifest)

    @property
    def intervals(self) -> list[str]:
        return [k for k, v in self._bars.items() if not v.empty]

    def symbols(self, interval: str) -> list[str]:
        return sorted(self._bars[interval]["symbol"].unique().tolist())

    def coverage(self, interval: str) -> dict[str, dict]:
        frame = self._bars[interval]
        out = {}
        for symbol, group in frame.groupby("symbol"):
            out[str(symbol)] = {"rows": int(len(group)), "first": group.ts.min().isoformat(), "last": group.ts.max().isoformat()}
        return out

    def bars(self, interval: str, symbols: list[str], start: pd.Timestamp, end_exclusive: pd.Timestamp) -> pd.DataFrame:
        frame = self._bars[interval]
        mask = frame.symbol.isin(symbols) & (frame.ts >= start) & (frame.ts < end_exclusive)
        return frame.loc[mask].reset_index(drop=True)

    def news(self, symbols: list[str], end_exclusive: pd.Timestamp, lookback_start: pd.Timestamp | None = None) -> pd.DataFrame:
        if self._news.empty:
            return self._news
        frame = self._news
        mask = frame.symbol.isin(symbols) & (frame.available_at < end_exclusive)
        if lookback_start is not None:
            mask &= frame.available_at >= lookback_start
        return frame.loc[mask].reset_index(drop=True)

    def news_counts(self) -> dict:
        if self._news.empty:
            return {"rows": 0, "by_symbol": {}, "by_provider": {}}
        return {
            "rows": int(len(self._news)),
            "by_symbol": {str(k): int(v) for k, v in self._news.groupby("symbol").size().items()},
            "by_provider": {str(k): int(v) for k, v in self._news.groupby("provider").size().items()},
        }


@dataclass
class SymbolSeries:
    ts: np.ndarray        # bar start, int64 ns UTC
    bar_end: np.ndarray   # completion time, int64 ns UTC
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    index_by_ts: dict[int, int]


class MarketTimeline:
    """Decision boundaries and completed-bar lookups for one run."""

    def __init__(self, bars: pd.DataFrame, interval: str, decision_start: pd.Timestamp | None = None):
        """``bars`` may include history before ``decision_start``; decisions only happen at
        bar starts at or after it. Earlier bars serve as completed history for marks and the feed."""
        if bars.empty:
            raise SnapshotError("no bars in the requested window")
        self.interval = interval
        self.nominal_ns = NOMINAL_SECONDS[interval] * 1_000_000_000
        bars = _ns(bars, ["ts"])
        all_ts = np.sort(pd.DatetimeIndex(bars["ts"].unique()).as_unit("ns").asi8)
        if decision_start is not None:
            all_ts = all_ts[all_ts >= int(decision_start.value)]
        if len(all_ts) == 0:
            raise SnapshotError("no decision boundaries in the requested window")
        self.boundaries: np.ndarray = all_ts
        self.series: dict[str, SymbolSeries] = {}
        for symbol, group in bars.groupby("symbol"):
            group = group.sort_values("ts")
            ts = pd.DatetimeIndex(group["ts"]).as_unit("ns").asi8
            nxt = np.append(ts[1:], ts[-1:] + self.nominal_ns)
            bar_end = np.minimum(ts + self.nominal_ns, nxt)
            self.series[str(symbol)] = SymbolSeries(
                ts=ts,
                bar_end=bar_end,
                open=group["open"].to_numpy(dtype=float),
                high=group["high"].to_numpy(dtype=float),
                low=group["low"].to_numpy(dtype=float),
                close=group["close"].to_numpy(dtype=float),
                volume=group["volume"].to_numpy(dtype="int64"),
                index_by_ts={int(t): i for i, t in enumerate(ts)},
            )

    @property
    def steps(self) -> int:
        return len(self.boundaries)

    def boundary(self, step: int) -> pd.Timestamp:
        return pd.Timestamp(int(self.boundaries[step]), unit="ns", tz="UTC")

    def window_end(self, step: int) -> pd.Timestamp:
        if step + 1 < self.steps:
            return self.boundary(step + 1)
        return pd.Timestamp(int(self.boundaries[step] + self.nominal_ns), unit="ns", tz="UTC")

    def open_at(self, symbol: str, t) -> float | None:
        series = self.series.get(symbol)
        if series is None:
            return None
        idx = series.index_by_ts.get(int(_ts(t).value))
        return None if idx is None else float(series.open[idx])

    def last_close_through(self, symbol: str, t) -> tuple[float, pd.Timestamp] | None:
        """Close of the last bar that completed at or before ``t``."""
        series = self.series.get(symbol)
        if series is None:
            return None
        idx = int(np.searchsorted(series.bar_end, int(_ts(t).value), side="right")) - 1
        if idx < 0:
            return None
        return float(series.close[idx]), pd.Timestamp(int(series.ts[idx]), unit="ns", tz="UTC")

    def completed_bars(self, symbol: str, t, limit: int | None = None) -> pd.DataFrame:
        series = self.series.get(symbol)
        if series is None:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "bar_end"])
        n = int(np.searchsorted(series.bar_end, int(_ts(t).value), side="right"))
        lo = 0 if limit is None else max(0, n - limit)
        return pd.DataFrame(
            {
                "ts": pd.to_datetime(series.ts[lo:n], utc=True),
                "open": series.open[lo:n],
                "high": series.high[lo:n],
                "low": series.low[lo:n],
                "close": series.close[lo:n],
                "volume": series.volume[lo:n],
                "bar_end": pd.to_datetime(series.bar_end[lo:n], utc=True),
            }
        )

    def last_bar_close_in_run(self, symbol: str) -> float | None:
        series = self.series.get(symbol)
        return None if series is None else float(series.close[-1])

    def has_decision_bars(self, symbol: str) -> bool:
        series = self.series.get(symbol)
        return series is not None and bool((series.ts >= self.boundaries[0]).any())
