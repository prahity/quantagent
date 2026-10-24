"""Append-only, run-scoped experience log.

Every record carries an `available_at` timestamp: the simulated time at which the
experience becomes usable (for a decision's outcome, that is the time the simulator
released the feedback). Retrieval at `as_of` returns only records with
`available_at <= as_of`, so a result learned at 14:05 can never influence the 14:01
decision. The log is append-only: a correction is a new record, never an edit.

Run-scoped: a fresh file per run_id. Nothing from another run is readable here. Imports
no simulator and no model code.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _epoch(ts) -> float:
    if ts is None:
        return float("inf")
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class Memory:
    def __init__(self, path: str | Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._records: list[dict] = []
        if self.path.exists():
            self.path.unlink()  # fresh memory every run

    def append(self, record: dict) -> dict:
        """Append an experience record. `available_at` (ISO-8601) is required and gates
        retrieval. The record is scoped to this run."""
        if "available_at" not in record:
            raise ValueError("memory records must carry an `available_at` cutoff")
        stored = {"run_id": self.run_id, "seq": len(self._records), **record}
        self._records.append(stored)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(stored, default=str) + "\n")
        return stored

    def retrieve(self, as_of: str, limit: int | None = None, kind: str | None = None) -> list[dict]:
        """Return eligible records (available_at <= as_of), newest first, scoped to this run."""
        cutoff = _epoch(as_of)
        eligible = [
            r for r in self._records
            if _epoch(r.get("available_at")) <= cutoff and (kind is None or r.get("kind") == kind)
        ]
        eligible.sort(key=lambda r: (_epoch(r.get("available_at")), r.get("seq", 0)), reverse=True)
        return eligible if limit is None else eligible[:limit]

    def __len__(self) -> int:
        return len(self._records)
