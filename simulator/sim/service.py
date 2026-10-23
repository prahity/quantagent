"""JSON-in / JSON-out simulator service with per-run persistence and replay.

This is the only entry point the runner and the HTTP layer use. It never imports agent code.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from contracts.research import LiveCollection
from sim.config import RUNS_DIR
from sim.engine import Engine, RunConfig
from sim.research_archive import ResearchArchive
from sim.store import SnapshotStore


def _dump(model: BaseModel) -> dict:
    return model.model_dump(mode="json")


class SimulatorService:
    def __init__(self, store: SnapshotStore | None = None, runs_dir: Path = RUNS_DIR, persist: bool = True,
                 research_archive: ResearchArchive | None = None):
        self.store = store or SnapshotStore.load()
        self.runs_dir = runs_dir
        self.persist = persist
        self.engines: dict[str, Engine] = {}
        self.research_archive = research_archive or ResearchArchive()

    # ------------------------------------------------------------------ runs
    def reset(self, run_config: dict[str, Any] | RunConfig) -> dict:
        config = run_config if isinstance(run_config, RunConfig) else RunConfig.from_defaults(run_config)
        if config.run_id in self.engines:
            raise ValueError(f"run_id {config.run_id} already exists")
        if self.persist and self._run_dir(config.run_id).exists():
            raise ValueError(f"run_id {config.run_id} already has persisted artifacts; use a new run_id")
        collection = self.research_archive.load(config.research.collection_id) if config.research.collection_id else None
        if collection is not None and not set(config.symbols) <= set(collection.symbols):
            raise ValueError("research collection must cover every run symbol")
        engine = Engine(config, self.store, research_collection=collection)
        request = engine.reset()
        self.engines[config.run_id] = engine
        if self.persist:
            run_dir = self._run_dir(config.run_id)
            run_dir.mkdir(parents=True)
            (run_dir / "config.json").write_text(json.dumps(_dump(config), indent=2))
            if collection is not None:
                (run_dir / "research_collection.json").write_text(collection.model_dump_json(indent=2))
            self._append(config.run_id, "requests.jsonl", _dump(request))
            self._persist_research(engine, request.request_id)
        return _dump(request)

    def submit_action(self, run_id: str, payload: Any) -> dict:
        engine = self._engine(run_id)
        ack, next_request, report = engine.submit_action(payload)
        if self.persist:
            self._append(run_id, "actions.jsonl", {"request_id": ack.request_id, "payload": payload, "ack": _dump(ack)})
            if next_request is not None:
                self._append(run_id, "requests.jsonl", _dump(next_request))
                self._persist_research(engine, next_request.request_id)
            if report is not None:
                self._write_final(run_id, engine)
        return {
            "ack": _dump(ack),
            "next_request": None if next_request is None else _dump(next_request),
            "report": None if report is None else _dump(report),
        }

    def state(self, run_id: str) -> dict:
        return self._engine(run_id).state()

    def list_runs(self) -> list[dict]:
        out = []
        for engine in self.engines.values():
            s = engine.state()
            out.append({k: s[k] for k in ("run_id", "label", "status", "step", "steps_total", "as_of", "cash", "equity", "fills", "rejections", "symbols")} | {
                "interval": engine.config.interval, "mode": engine.config.mode, "initial_cash": engine.config.initial_cash,
                "start": engine.config.start, "end": engine.config.end,
                "net_profit": None if engine.report is None else engine.report.net_profit,
            })
        return out

    def report(self, run_id: str) -> dict | None:
        report = self._engine(run_id).report
        return None if report is None else _dump(report)

    def equity_curve(self, run_id: str) -> list[dict]:
        return [_dump(p) for p in self._engine(run_id).equity_curve()]

    def fills(self, run_id: str) -> list[dict]:
        return [_dump(f) for f in self._engine(run_id).ledger.fills]

    def rejections(self, run_id: str) -> list[dict]:
        return [_dump(r) for r in self._engine(run_id).ledger.rejections]

    def records(self, run_id: str) -> list[dict]:
        return [_dump(r) for r in self._engine(run_id).records]

    def feed_prices(self, run_id: str, symbol: str, limit: int | None = None) -> dict:
        engine = self._engine(run_id)
        frame = engine.feed.prices(symbol, limit=limit)
        bars = frame.assign(ts=frame.ts.map(lambda x: x.isoformat()), bar_end=frame.bar_end.map(lambda x: x.isoformat()))
        return {"symbol": symbol, "as_of": engine.feed.clock.isoformat(), "bars": bars.to_dict(orient="records")}

    def feed_news(self, run_id: str, symbols: list[str] | None = None) -> dict:
        engine = self._engine(run_id)
        frame = engine.feed.news(symbols=symbols)
        rows = []
        for row in frame.to_dict(orient="records"):
            rows.append({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items() if k not in ("retrieved_at", "content_version", "filing_date", "items", "acceptance_api_raw")})
        return {"as_of": engine.feed.clock.isoformat(), "records": rows}

    def feed_marks(self, run_id: str) -> dict:
        engine = self._engine(run_id)
        return {"as_of": engine.feed.clock.isoformat(), "marks": engine.feed.marks()}

    def feed_research(self, run_id: str, request_id: str | None = None) -> dict | None:
        engine = self._engine(run_id)
        if not engine.config.research.enabled:
            return None
        if request_id is None:
            request_id = f"decision_{engine.step:05d}"
        if request_id not in engine.research_briefs:
            raise KeyError("research is only available for emitted decisions")
        return _dump(engine.research_briefs[request_id])

    def upcoming_news_count(self, run_id: str) -> int:
        engine = self._engine(run_id)
        return engine.news.total() - engine.news.released_count()

    # ------------------------------------------------------------------ replay
    def replay(self, run_id: str) -> dict:
        """Re-run the recorded action log on a fresh engine and compare the official result."""
        run_dir = self._run_dir(run_id)
        config_data = json.loads((run_dir / "config.json").read_text())
        config = RunConfig(**config_data)
        actions = [json.loads(line) for line in (run_dir / "actions.jsonl").read_text().splitlines() if line.strip()]
        original = json.loads((run_dir / "report.json").read_text()) if (run_dir / "report.json").exists() else None

        # Replay uses the per-run pinned collection, never a refreshed live search.
        collection_path = run_dir / "research_collection.json"
        collection = LiveCollection.model_validate_json(collection_path.read_text()) if collection_path.exists() else None
        if config.research.collection_id and (collection is None or collection.collection_id != config.research.collection_id):
            raise ValueError("pinned research collection is missing or does not match the run")
        engine = Engine(config, self.store, research_collection=collection)
        engine.reset()
        report = None
        for entry in actions:
            if engine.status != "awaiting_action":
                break
            _, _, report = engine.submit_action(entry["payload"])
        replayed = None if report is None else _dump(report)
        keys = ["final_cash", "final_equity", "net_profit", "fills", "rejections", "max_drawdown_pct"]
        match = original is not None and replayed is not None and all(original[k] == replayed[k] for k in keys)
        research_path = run_dir / "research.jsonl"
        research_match = False if config_data.get("research", {}).get("enabled", False) else None
        if research_path.exists():
            recorded = [json.loads(line) for line in research_path.read_text().splitlines() if line.strip()]
            research_match = recorded == [_dump(b) for b in engine.research_briefs.values()]
        return {
            "run_id": run_id,
            "actions_replayed": len(actions),
            "match": match and research_match is not False,
            "account_match": match,
            "research_match": research_match,
            "original": None if original is None else {k: original[k] for k in keys},
            "replayed": None if replayed is None else {k: replayed[k] for k in keys},
        }

    # ------------------------------------------------------------------ helpers
    def _engine(self, run_id: str) -> Engine:
        if run_id not in self.engines:
            raise KeyError(f"unknown run_id {run_id}")
        return self.engines[run_id]

    def _run_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
            raise ValueError("invalid run id")
        return self.runs_dir / run_id

    def _append(self, run_id: str, name: str, payload: dict) -> None:
        with (self._run_dir(run_id) / name).open("a") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")

    def _persist_research(self, engine: Engine, request_id: str) -> None:
        brief = engine.research_briefs.get(request_id)
        if brief is not None:
            self._append(engine.config.run_id, "research.jsonl", _dump(brief))

    def _write_final(self, run_id: str, engine: Engine) -> None:
        run_dir = self._run_dir(run_id)
        (run_dir / "report.json").write_text(json.dumps(_dump(engine.report), indent=2))
        (run_dir / "fills.jsonl").write_text("".join(json.dumps(_dump(f)) + "\n" for f in engine.ledger.fills))
        (run_dir / "rejections.jsonl").write_text("".join(json.dumps(_dump(r)) + "\n" for r in engine.ledger.rejections))
        (run_dir / "equity.jsonl").write_text("".join(json.dumps(_dump(p)) + "\n" for p in engine.equity_curve()))
