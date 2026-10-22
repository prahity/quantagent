from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SNAPSHOT_DIR = ROOT / "sim" / "snapshot"
RUNS_DIR = ROOT / "runs"
MANIFEST_PATH = CONFIG_DIR / "snapshot_manifest.json"

INTERVALS = {"hourly": "1h", "daily": "1d"}


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def load_environment() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "environment.yaml")


def load_universe() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "universe.yaml")


def load_splits() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "splits.yaml")


def universe_symbols() -> list[str]:
    return [s["ticker"] for s in load_universe()["symbols"]]


def universe_by_ticker() -> dict[str, dict[str, Any]]:
    return {s["ticker"]: s for s in load_universe()["symbols"]}


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out
