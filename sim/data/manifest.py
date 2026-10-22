"""Pinned description of the frozen snapshot: hashes, coverage and provenance."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _coverage(frame: pd.DataFrame, key: str, ts_col: str) -> dict:
    out = {}
    if frame.empty:
        return out
    for value, group in frame.groupby(key):
        out[str(value)] = {
            "rows": int(len(group)),
            "first": group[ts_col].min().isoformat(),
            "last": group[ts_col].max().isoformat(),
        }
    return out


def build_manifest(snapshot_dir: Path, bars: dict[str, pd.DataFrame], news: pd.DataFrame, universe: dict, sources: dict) -> dict:
    files = {}
    for name in sorted(p.name for p in snapshot_dir.glob("*.parquet")):
        path = snapshot_dir / name
        files[name] = {"sha256": sha256_of(path), "bytes": path.stat().st_size}
    combined = hashlib.sha256("".join(v["sha256"] for v in files.values()).encode()).hexdigest()[:16]
    return {
        "data_version": f"snap-{combined}",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data_start": universe["data_start"],
        "data_end": universe["data_end"],
        "symbols": [s["ticker"] for s in universe["symbols"]],
        "sources": sources,
        "files": files,
        "bars": {interval: _coverage(frame, "symbol", "ts") for interval, frame in bars.items()},
        "news": {
            "rows": int(len(news)),
            "by_provider": {str(k): int(v) for k, v in news.groupby("provider").size().items()} if not news.empty else {},
            "by_symbol": _coverage(news, "symbol", "available_at"),
        },
    }


def write_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def read_manifest(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())
