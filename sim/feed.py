"""The released data interface: everything an agent may read, pinned to an as_of.

Later this is what gets published into Zetaris under the simulator identity. Reads for
a time later than the run's current clock are refused; the agent cannot peek ahead by
asking for a different cutoff.
"""
from __future__ import annotations

import pandas as pd

from sim.news_queue import NewsQueue
from sim.store import MarketTimeline


class FutureAccessError(PermissionError):
    pass


class Feed:
    def __init__(self, market: MarketTimeline, news: NewsQueue, symbols: list[str]):
        self._market = market
        self._news = news
        self._symbols = symbols
        self._clock: pd.Timestamp | None = None

    def set_clock(self, as_of: pd.Timestamp) -> None:
        self._clock = as_of

    @property
    def clock(self) -> pd.Timestamp | None:
        return self._clock

    def _check(self, as_of: pd.Timestamp | None) -> pd.Timestamp:
        if self._clock is None:
            raise FutureAccessError("run has not started")
        as_of = self._clock if as_of is None else as_of
        if as_of > self._clock:
            raise FutureAccessError(f"as_of {as_of.isoformat()} is after the run clock {self._clock.isoformat()}")
        return as_of

    def prices(self, symbol: str, as_of: pd.Timestamp | None = None, limit: int | None = None) -> pd.DataFrame:
        as_of = self._check(as_of)
        if symbol not in self._symbols:
            raise KeyError(f"{symbol} is not in this run's universe")
        return self._market.completed_bars(symbol, as_of, limit=limit)

    def news(self, as_of: pd.Timestamp | None = None, symbols: list[str] | None = None) -> pd.DataFrame:
        as_of = self._check(as_of)
        return self._news.eligible(as_of, symbols or self._symbols)

    def marks(self, as_of: pd.Timestamp | None = None) -> dict[str, dict]:
        as_of = self._check(as_of)
        out = {}
        for symbol in self._symbols:
            last = self._market.last_close_through(symbol, as_of)
            if last is not None:
                out[symbol] = {"close": last[0], "bar_ts": last[1].isoformat()}
        return out
