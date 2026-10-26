"""Connect the independent simulator to the trading agent through the fixed JSON contract.

    python runner.py --agent momentum --minutes 10
    python runner.py --agent nemotron --minutes 30 --interval hourly --start 2025-01-28 --end 2025-01-31

This runner is the ONLY module that imports both the simulator and the agent. The simulator
already exposes the architecture's contract (reset(run_config) -> decision_request,
submit_action(action_json) -> {ack, next_request, report}), so no behavioural adapter is
needed; the runner only (a) builds the per-run released-feed SQLite database the query tool
reads, (b) binds a fresh query tool to each decision's as_of, and (c) asserts, before every
decision, that the feed holds no row released after the cutoff.

Every run gets a fresh run_id, a fresh feed database and fresh memory.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SIMULATOR = ROOT / "simulator"
RUNS_DIR = ROOT / "runs"

# The runner may import the simulator; the agent/ and baselines/ packages may not.
# ROOT must win name resolution so the root `agent` package is not shadowed by the
# simulator's own `agent/` test harness. ROOT is already first on sys.path (the script
# dir); the simulator is appended so only its unique packages (`sim`, `contracts`) resolve.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SIMULATOR) not in sys.path:
    sys.path.append(str(SIMULATOR))

from agent.config import QueryConfig, architecture_path  # noqa: E402
from agent.memory import Memory  # noqa: E402
from agent.query_tool import FeedScope, QueryTool, build_feed_db, make_backend  # noqa: E402

PRICE_COLS = ["symbol", "ts", "open", "high", "low", "close", "volume", "bar_end", "released_at"]
NEWS_COLS = ["event_id", "symbol", "provider", "form", "title", "summary", "source_url",
             "published_at", "available_at", "released_at"]


# ------------------------------------------------------------------ simulator store
def build_service(data: str = "auto", persist: bool = True):
    """Return a SimulatorService backed by the frozen snapshot, or the real fixture slice."""
    from sim.service import SimulatorService
    from sim.store import SnapshotError, SnapshotStore

    if data in ("auto", "snapshot"):
        try:
            return SimulatorService(SnapshotStore.load(), runs_dir=RUNS_DIR / "_sim", persist=persist), "snapshot"
        except SnapshotError:
            if data == "snapshot":
                raise
    # fixtures: a real cut of the snapshot shipped with the simulator tests
    import pandas as pd

    fx = SIMULATOR / "tests" / "fixtures"
    bars = {i: pd.read_parquet(fx / f"bars_{i}.parquet") for i in ("hourly", "daily")}
    news = pd.read_parquet(fx / "news.parquet")
    store = SnapshotStore(bars, news, data_version="fixture-2025-01-27..2025-02-07")
    return SimulatorService(store, runs_dir=RUNS_DIR / "_sim", persist=persist), "fixtures"


def _native(value):
    return value.item() if hasattr(value, "item") else value


def collect_feed(service, run_id: str, symbols: list[str], as_of: str) -> tuple[list[dict], list[dict]]:
    """Pull the released feed from the simulator and shape it for the query db.

    A price bar is released once it has completed, so `released_at` = `bar_end`. News
    records already carry the simulator's `released_at`. Asserts the invariant that no
    released row is dated after the decision cutoff.
    """
    prices: list[dict] = []
    for symbol in symbols:
        for bar in service.feed_prices(run_id, symbol)["bars"]:
            row = {k: _native(bar.get(k)) for k in ("ts", "open", "high", "low", "close", "volume", "bar_end")}
            row["symbol"] = symbol
            row["released_at"] = row["bar_end"]
            prices.append({k: row.get(k) for k in PRICE_COLS})

    news: list[dict] = []
    for rec in service.feed_news(run_id)["records"]:
        news.append({k: rec.get(k) for k in NEWS_COLS})

    for row in prices + news:
        released = row.get("released_at")
        assert released is not None and released <= as_of, (
            f"feed invariant violated: released_at {released!r} > as_of {as_of!r}"
        )
    return prices, news


def feed_scope(state: dict) -> FeedScope:
    """The rows the simulator released to this run, from its resolved config.

    Mirrors Engine.__init__: price history starts `history_lookback_days` before `start`
    (on bar `ts`), news `news_lookback_days` before it (on `available_at`). The Zetaris
    tables hold the whole snapshot, so the query tool applies this to match the SQLite feed.
    """
    config = state["config"]
    start = datetime.fromisoformat(config["start"]).replace(tzinfo=timezone.utc)
    return FeedScope(
        symbols=tuple(state["symbols"]),
        from_epoch={
            "prices": int((start - timedelta(days=config["history_lookback_days"])).timestamp()),
            "news": int((start - timedelta(days=config["news_lookback_days"])).timestamp()),
        },
    )


# ------------------------------------------------------------------ agent construction
def build_agent(name: str, run_dir: Path):
    if name == "momentum":
        from baselines.momentum import MomentumAgent

        return MomentumAgent()
    if name == "nemotron":
        from agent.model import NemotronModel
        from agent.policy import NemotronPolicy

        arch = architecture_path()
        text = arch.read_text(encoding="utf-8") if arch else ""
        return NemotronPolicy(NemotronModel(), workspace_dir=run_dir / "workspace", architecture_text=text)
    raise ValueError(f"unknown agent {name!r}; expected momentum | nemotron")


# ------------------------------------------------------------------ the run loop
def run(
    agent_name: str,
    minutes: int,
    run_config: dict | None = None,
    service=None,
    data: str = "auto",
    backend_kind: str | None = None,
    verbose: bool = True,
    backend=None,
) -> dict:
    """Run one agent against a fresh simulator account. `backend` injects a QueryBackend
    (tests); otherwise one is built from `backend_kind` / QUERY_BACKEND."""
    run_config = dict(run_config or {})
    run_id = run_config.get("run_id") or f"run_{uuid.uuid4().hex[:8]}"
    run_config["run_id"] = run_id
    run_config.setdefault("start", "2025-01-28")
    run_config.setdefault("end", "2025-01-31")

    data_label = "injected"
    if service is None:
        service, data_label = build_service(data=data)
    if data_label == "fixtures" and not run_config.get("symbols"):
        # The shipped fixture slice only covers these symbols.
        run_config["symbols"] = ["AAPL", "MSFT", "SPY"]

    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    query_log = run_dir / "query_log.jsonl"
    feed_db = run_dir / "feed.sqlite"
    memory = Memory(run_dir / "memory.jsonl", run_id)

    agent = build_agent(agent_name, run_dir)
    request = service.reset(run_config)
    state = service.state(run_id)
    scope = feed_scope(state)
    if backend is None:
        backend = make_backend(db_path=feed_db, backend=backend_kind, interval=state["config"]["interval"])
    published = backend.published_feed
    if published:
        # The Zetaris feed must be the same frozen snapshot the simulator is replaying, and
        # only that one: the feed CTEs do not filter on data_version.
        loaded = backend.loaded_data_versions()
        if loaded != {state["data_version"]}:
            backend.close()
            raise RuntimeError(
                f"Zetaris feed does not hold data_version {state['data_version']!r} (found {sorted(loaded)}). "
                "Run simulator/scripts/publish_zetaris.py first."
            )
    if hasattr(agent, "start_run"):
        agent.start_run(run_id, request)

    step = 0
    report = None
    while request is not None and step < minutes:
        as_of = request["as_of"]
        request_id = request["request_id"]
        symbols = request["data"]["symbols"]

        # collect_feed also asserts that nothing released after as_of is in the feed.
        prices, news = collect_feed(service, run_id, symbols, as_of)
        if not published:
            build_feed_db(feed_db, prices, news)

        query_tool = QueryTool(backend, run_id, request_id, as_of, query_log, QueryConfig(), scope=scope)
        payload = agent.decide(request, query_tool, memory)
        result = service.submit_action(run_id, payload)
        ack = result["ack"]
        next_request = result["next_request"]
        report = result["report"]

        available_at = next_request["as_of"] if next_request else request["window_end"]
        # LLM agents expose per-decision wall time / token totals; baselines don't.
        decision_stats = getattr(agent, "last_decision_stats", None) or {}
        memory.append({
            "kind": "decision",
            "request_id": request_id,
            "as_of": as_of,
            "available_at": available_at,
            "accepted": ack["accepted"],
            "reason": payload.get("reason") if isinstance(payload, dict) else None,
            "order_outcomes": ack.get("orders", []),
            "queries_used": query_tool.calls_used,
            **decision_stats,
        })

        if verbose:
            tag = "ok " if ack["accepted"] else "REJ"
            outs = ", ".join(f"{o['order_id']}:{o['status']}" for o in ack.get("orders", [])) or "hold"
            print(f"{step:4d} {as_of} {tag} q={query_tool.calls_used} {outs}")

        request = next_request
        step += 1
        if report is not None:
            break

    backend.close()
    summary = _summarize(service, run_id, report, step, query_log, data_label, agent_name)
    model = getattr(agent, "model", None)
    summary["model"] = model.settings() if hasattr(model, "settings") else None
    (run_dir / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def _summarize(service, run_id: str, report, steps_done: int, query_log: Path, data_label: str, agent_name: str) -> dict:
    q_total = q_errors = q_rows = 0
    if query_log.exists():
        for line in query_log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            q_total += 1
            q_errors += 1 if entry.get("error") else 0
            q_rows += entry.get("row_count") or 0

    state = service.state(run_id)
    official = report is not None
    equity = report["final_equity"] if official else state["equity"]
    initial = report["initial_cash"] if official else state["config"]["initial_cash"]
    return {
        "run_id": run_id,
        "agent": agent_name,
        "data": data_label,
        "data_version": state.get("data_version"),
        "steps_done": steps_done,
        "steps_total": state.get("steps_total"),
        "result_kind": "official_report" if official else "interim_state",
        "initial_cash": initial,
        "final_equity": round(equity, 4),
        "net_profit": round(equity - initial, 4),
        "fills": state.get("fills"),
        "rejections": state.get("rejections"),
        "invalid_responses": state.get("invalid_responses"),
        "positions": state.get("positions"),
        "queries": {"total": q_total, "errors": q_errors, "rows_returned": q_rows},
        "report": report,
    }


# ------------------------------------------------------------------ cli
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", required=True, choices=["momentum", "nemotron"])
    parser.add_argument("--minutes", type=int, default=10, help="number of decision steps (one per completed bar)")
    parser.add_argument("--interval", default="hourly", choices=["hourly", "daily"])
    parser.add_argument("--mode", default="V1", choices=["V1", "V2"])
    parser.add_argument("--start", default="2025-01-28", help="YYYY-MM-DD inclusive")
    parser.add_argument("--end", default="2025-01-31", help="YYYY-MM-DD inclusive")
    parser.add_argument("--symbols", help="comma-separated subset of the universe")
    parser.add_argument("--cash", type=float, default=None)
    parser.add_argument("--data", default="auto", choices=["auto", "snapshot", "fixtures"])
    parser.add_argument("--backend", default=None, choices=["sqlite", "zetaris"])
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    run_config = {
        "interval": args.interval,
        "mode": args.mode,
        "start": args.start,
        "end": args.end,
        "initial_cash": args.cash,
        "symbols": args.symbols.split(",") if args.symbols else None,
        "label": f"{args.agent}",
    }
    run_config = {k: v for k, v in run_config.items() if v is not None}
    summary = run(args.agent, args.minutes, run_config, data=args.data, backend_kind=args.backend, verbose=not args.quiet)
    show = {k: summary[k] for k in (
        "run_id", "agent", "data", "result_kind", "steps_done", "steps_total",
        "initial_cash", "final_equity", "net_profit", "fills", "rejections", "queries", "model",
    )}
    print(json.dumps(show, indent=2, default=str))


if __name__ == "__main__":
    main()
