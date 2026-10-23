"""Offline, HOLD-only research-loop demonstration on the committed real-data slice.

    python scripts/demo_research.py

No keys, full snapshot download, or external calls are required.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sim.service import SimulatorService  # noqa: E402
from sim.store import SnapshotStore  # noqa: E402


def main() -> None:
    fixtures = ROOT / "tests" / "fixtures"
    store = SnapshotStore(
        {interval: pd.read_parquet(fixtures / f"bars_{interval}.parquet") for interval in ("hourly", "daily")},
        pd.read_parquet(fixtures / "news.parquet"), "fixture-2025-01-27..2025-02-07",
    )
    service = SimulatorService(store)
    request = service.reset({"label": "Offline research-loop demo (HOLD)",
                             "start": "2025-01-28", "end": "2025-01-31", "symbols": ["AAPL", "MSFT"]})
    run_id = request["run_id"]
    print(json.dumps({"first_brief": service.feed_research(run_id)}, indent=2))
    while request is not None:
        payload = {key: request[key] for key in ("schema_version", "run_id", "request_id", "mode", "window_start", "window_end")}
        payload.update(orders=[], reason="HOLD-only research-loop demonstration")
        result = service.submit_action(run_id, payload)
        request = result["next_request"]
    print(json.dumps({"run_id": run_id, "decisions": result["report"]["steps"],
                      "replay": service.replay(run_id), "artifacts": str(ROOT / "runs" / run_id)}, indent=2))


if __name__ == "__main__":
    main()
