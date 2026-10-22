"""FastAPI surface for the simulator and the bundled test agents.

    uvicorn server.app:app --host 127.0.0.1 --port 8711 --reload

Everything an agent could do over this API is also possible in-process through
``sim.service.SimulatorService``; the web UI is just another client of these routes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.scripted import SCRIPTS_DIR, list_scripts, load_agent  # noqa: E402
from sim.config import load_environment, load_splits, load_universe  # noqa: E402
from sim.feed import FutureAccessError  # noqa: E402
from sim.service import SimulatorService  # noqa: E402
from sim.store import SnapshotError, SnapshotStore  # noqa: E402

app = FastAPI(title="AI Trader Simulator", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_service: SimulatorService | None = None
_snapshot_error: str | None = None


def service() -> SimulatorService:
    global _service, _snapshot_error
    if _service is None:
        try:
            _service = SimulatorService(SnapshotStore.load())
        except SnapshotError as exc:
            _snapshot_error = str(exc)
            raise HTTPException(status_code=503, detail=f"snapshot unavailable: {exc}")
    return _service


class RunCreate(BaseModel):
    run_id: str | None = None
    label: str | None = None
    interval: str | None = None
    mode: str | None = None
    initial_cash: float | None = None
    start: str
    end: str
    symbols: list[str] | None = None
    time_scale: float | None = None
    execution: dict[str, Any] | None = None
    news: dict[str, Any] | None = None
    history_lookback_days: int | None = None
    news_lookback_days: int | None = None


class Autoplay(BaseModel):
    agent: str = Field(default="hold", description="hold | buy_and_hold | script")
    script: str | None = None
    steps: int = Field(default=1, ge=1, le=10_000)


class _LocalFeed:
    def __init__(self, svc: SimulatorService, run_id: str):
        self._svc, self._run_id = svc, run_id

    def prices(self, symbol: str, limit: int | None = None):
        return self._svc.feed_prices(self._run_id, symbol, limit)["bars"]

    def news(self, symbols: list[str] | None = None):
        return self._svc.feed_news(self._run_id, symbols)["records"]

    def marks(self):
        return self._svc.feed_marks(self._run_id)["marks"]


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FutureAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (ValueError, SnapshotError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/health")
def health() -> dict:
    try:
        svc = service()
    except HTTPException as exc:
        return {"ok": False, "detail": exc.detail}
    return {"ok": True, "data_version": svc.store.data_version, "runs": len(svc.engines)}


@app.get("/api/config")
def config() -> dict:
    svc = service()
    universe = load_universe()
    return {
        "environment": load_environment(),
        "universe": universe,
        "splits": load_splits(),
        "snapshot": {
            "data_version": svc.store.data_version,
            "manifest": svc.store.manifest,
            "coverage": {interval: svc.store.coverage(interval) for interval in svc.store.intervals},
            "news": svc.store.news_counts(),
        },
        "agents": ["hold", "buy_and_hold", "script"],
        "scripts": list_scripts(),
    }


@app.get("/api/scripts")
def scripts() -> list[dict]:
    return list_scripts()


@app.get("/api/scripts/{script_id}")
def script(script_id: str) -> dict:
    path = SCRIPTS_DIR / f"{script_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="unknown script")
    return json.loads(path.read_text())


@app.get("/api/runs")
def runs() -> list[dict]:
    return service().list_runs()


@app.post("/api/runs", status_code=201)
def create_run(body: RunCreate) -> dict:
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    return _wrap(service().reset, payload)


@app.get("/api/runs/{run_id}")
def run_state(run_id: str) -> dict:
    return _wrap(service().state, run_id)


@app.post("/api/runs/{run_id}/actions")
def submit_action(run_id: str, payload: Any = None) -> dict:
    return _wrap(service().submit_action, run_id, payload)


@app.post("/api/runs/{run_id}/autoplay")
def autoplay(run_id: str, body: Autoplay) -> dict:
    svc = service()
    agent = _wrap(load_agent, body.agent, body.script)
    feed = _LocalFeed(svc, run_id)
    state = _wrap(svc.state, run_id)
    done = 0
    last = None
    while done < body.steps and state["status"] == "awaiting_action":
        payload = agent.decide(state["current_request"], feed)
        last = _wrap(svc.submit_action, run_id, payload)
        state = svc.state(run_id)
        done += 1
    return {"steps_done": done, "agent": agent.name, "state": state, "last": last}


@app.get("/api/runs/{run_id}/equity")
def equity(run_id: str) -> list[dict]:
    return _wrap(service().equity_curve, run_id)


@app.get("/api/runs/{run_id}/fills")
def fills(run_id: str) -> list[dict]:
    return _wrap(service().fills, run_id)


@app.get("/api/runs/{run_id}/rejections")
def rejections(run_id: str) -> list[dict]:
    return _wrap(service().rejections, run_id)


@app.get("/api/runs/{run_id}/records")
def records(run_id: str) -> list[dict]:
    return _wrap(service().records, run_id)


@app.get("/api/runs/{run_id}/report")
def report(run_id: str) -> dict:
    out = _wrap(service().report, run_id)
    if out is None:
        raise HTTPException(status_code=409, detail="run has not finished")
    return out


@app.get("/api/runs/{run_id}/replay")
def replay(run_id: str) -> dict:
    try:
        return service().replay(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no recorded run: {exc}")


@app.get("/api/runs/{run_id}/feed/prices")
def feed_prices(run_id: str, symbol: str, limit: int | None = Query(default=None, ge=1), as_of: str | None = None) -> dict:
    svc = service()
    if as_of is not None:
        import pandas as pd

        engine = _wrap(svc._engine, run_id)
        frame = _wrap(engine.feed.prices, symbol, pd.Timestamp(as_of).tz_convert("UTC") if pd.Timestamp(as_of).tzinfo else pd.Timestamp(as_of, tz="UTC"), limit)
        return {"symbol": symbol, "as_of": as_of, "bars": json.loads(frame.to_json(orient="records", date_format="iso"))}
    return _wrap(svc.feed_prices, run_id, symbol, limit)


@app.get("/api/runs/{run_id}/feed/news")
def feed_news(run_id: str, symbols: str | None = None) -> dict:
    return _wrap(service().feed_news, run_id, symbols.split(",") if symbols else None)


@app.get("/api/runs/{run_id}/feed/marks")
def feed_marks(run_id: str) -> dict:
    return _wrap(service().feed_marks, run_id)
