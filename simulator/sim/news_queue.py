"""Timestamped news/context events released as the simulated clock advances."""
from __future__ import annotations

import pandas as pd

RECORD_FIELDS = [
    "event_id", "symbol", "provider", "form", "title", "summary", "source_url",
    "published_at", "available_at", "availability_basis", "availability_verified",
]


class NewsQueue:
    def __init__(self, news: pd.DataFrame):
        if news is None or news.empty:
            news = pd.DataFrame(columns=RECORD_FIELDS)
        self._news = news.sort_values(["available_at", "event_id"]).reset_index(drop=True)
        self._released_at: dict[str, pd.Timestamp] = {}
        self._cursor = 0

    def release_through(self, t: pd.Timestamp) -> list[str]:
        """Stamp ``released_at = t`` on every record whose availability is reached. Returns new ids."""
        new_ids: list[str] = []
        n = len(self._news)
        while self._cursor < n:
            row = self._news.iloc[self._cursor]
            if row["available_at"] > t:
                break
            if row["published_at"] <= t and row["event_id"] not in self._released_at:
                self._released_at[row["event_id"]] = t
                new_ids.append(row["event_id"])
            self._cursor += 1
        return new_ids

    def released_at(self, event_id: str) -> pd.Timestamp | None:
        return self._released_at.get(event_id)

    def eligible(self, as_of: pd.Timestamp, symbols: list[str] | None = None) -> pd.DataFrame:
        """Records usable for a decision at ``as_of``: published, available and released by then."""
        frame = self._news
        if frame.empty:
            return frame
        ids = [eid for eid, rel in self._released_at.items() if rel <= as_of]
        out = frame[frame.event_id.isin(ids) & (frame.published_at <= as_of) & (frame.available_at <= as_of)]
        if symbols:
            out = out[out.symbol.isin(symbols)]
        out = out.copy()
        out["released_at"] = out["event_id"].map(self._released_at)
        return out.sort_values(["available_at", "event_id"], ascending=[False, True]).reset_index(drop=True)

    def digest(self, as_of: pd.Timestamp, held_symbols: list[str], universe: list[str], max_events: int, prioritize_positions: bool = True) -> list[str]:
        eligible = self.eligible(as_of, universe)
        if eligible.empty:
            return []
        if prioritize_positions and held_symbols:
            held = eligible[eligible.symbol.isin(held_symbols)]
            rest = eligible[~eligible.symbol.isin(held_symbols)]
            ordered = pd.concat([held, rest])
        else:
            ordered = eligible
        return ordered["event_id"].head(max_events).tolist()

    def total(self) -> int:
        return int(len(self._news))

    def released_count(self) -> int:
        return len(self._released_at)
