"""Connect a test agent to the simulator through the fixed JSON contract.

    python runner.py run --agent script --script aapl_round_trip --start 2025-01-27 --end 2025-01-31 --symbols AAPL,MSFT
    python runner.py run --agent buy_and_hold --interval daily --start 2025-01-02 --end 2025-03-31
    python runner.py run --agent hold --server http://127.0.0.1:8711 ...   # drive a running server instead
    python runner.py replay <run_id>
    python runner.py scripts

The runner is the only component that imports both ``sim`` and ``agent``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent.client import FeedReader, SimClient  # noqa: E402
from agent.scripted import list_scripts, load_agent  # noqa: E402


class LocalFeedReader:
    """In-process released-data reader bound to a run. Mirrors HttpFeedReader."""

    def __init__(self, service, run_id: str):
        self._service = service
        self._run_id = run_id

    def prices(self, symbol: str, limit: int | None = None) -> list[dict]:
        return self._service.feed_prices(self._run_id, symbol, limit)["bars"]

    def news(self, symbols: list[str] | None = None) -> list[dict]:
        return self._service.feed_news(self._run_id, symbols)["records"]

    def marks(self) -> dict[str, dict]:
        return self._service.feed_marks(self._run_id)["marks"]


def run_agent(agent, reset, submit, feed: FeedReader, run_config: dict, verbose: bool = True) -> dict:
    request = reset(run_config)
    run_id = request["run_id"]
    while request is not None:
        payload = agent.decide(request, feed)
        result = submit(run_id, payload)
        ack = result["ack"]
        if hasattr(agent, "observe"):
            agent.observe(request, payload, ack)
        if verbose:
            tag = "ok  " if ack["accepted"] else "REJ "
            orders = ", ".join(f"{o['order_id']}:{o['status']}{(' ' + o['reason']) if o['reason'] else ''}" for o in ack["orders"]) or "hold"
            errs = "; ".join(e["message"] for e in ack["errors"])
            print(f"{request['step']:5d} {request['as_of']} {tag} {orders}{(' | ' + errs) if errs else ''}")
        request = result["next_request"]
        if result["report"] is not None:
            return result["report"]
    raise RuntimeError("run ended without a report")


def cmd_run(args) -> None:
    run_config = {
        "interval": args.interval,
        "mode": args.mode,
        "start": args.start,
        "end": args.end,
        "initial_cash": args.cash,
        "symbols": args.symbols.split(",") if args.symbols else None,
        "label": args.label or f"{args.agent}{(':' + args.script) if args.script else ''}",
        "run_id": args.run_id,
    }
    run_config = {k: v for k, v in run_config.items() if v is not None}
    agent = _nemotron(args) if args.agent == "nemotron" else load_agent(args.agent, args.script)

    if args.server:
        client = SimClient(args.server)
        first = client.reset(run_config)
        feed = client.feed(first["run_id"])
        report = run_agent(agent, lambda _cfg: first, client.submit_action, feed, run_config, verbose=not args.quiet)
    else:
        from sim.service import SimulatorService

        service = SimulatorService()
        holder: dict = {}

        def reset(cfg):
            req = service.reset(cfg)
            holder["feed"] = LocalFeedReader(service, req["run_id"])
            return req

        first = reset(run_config)
        report = run_agent(agent, lambda _cfg: first, service.submit_action, holder["feed"], run_config, verbose=not args.quiet)

    keys = ["run_id", "label", "interval", "mode", "steps", "initial_cash", "final_equity", "net_profit", "return_pct", "max_drawdown_pct", "sharpe", "fills", "rejections", "invalid_responses", "data_version"]
    summary = {k: report[k] for k in keys}
    if args.agent == "nemotron":
        summary["model"] = agent.runtime.model
        summary["memory"] = agent.memory
    print(json.dumps(summary, indent=2))


def _nemotron(args):
    """Nemotron through Nightshift. Settings come from METERLESS_* in the environment."""
    sys.path.insert(0, str(ROOT.parent))
    from meterless.provider import MeterlessRuntime, TOKEN_SETTING, load_settings
    from meterless.trader import NemotronTrader

    settings = load_settings()
    runtime = MeterlessRuntime(settings["base_url"], settings[TOKEN_SETTING], args.model or settings["model"])
    return NemotronTrader(runtime, memory=args.memory)


def cmd_replay(args) -> None:
    from sim.service import SimulatorService

    service = SimulatorService()
    print(json.dumps(service.replay(args.run_id), indent=2))


def cmd_scripts(_args) -> None:
    for script in list_scripts():
        print(f"{script['id']:20} {script['commands']:3} commands  {script['description']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run an agent through a fresh simulator run")
    run.add_argument("--agent", default="hold", choices=["hold", "buy_and_hold", "script", "nemotron"])
    run.add_argument("--script", help="script id under agent/scripts/ or a path to a JSON file")
    run.add_argument("--model", help="nemotron only: pinned model id, e.g. nvidia/nemotron-3-super-120b-a12b")
    run.add_argument("--memory", type=int, default=0, help="nemotron only: earlier decisions from this run kept in the prompt")
    run.add_argument("--interval", default="hourly", choices=["hourly", "daily"])
    run.add_argument("--mode", default="V1", choices=["V1", "V2"])
    run.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    run.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    run.add_argument("--symbols", help="comma-separated subset of the universe")
    run.add_argument("--cash", type=float, default=None, help="initial_cash K (default from config/environment.yaml)")
    run.add_argument("--label")
    run.add_argument("--run-id")
    run.add_argument("--server", help="base URL of a running server, e.g. http://127.0.0.1:8711")
    run.add_argument("--quiet", action="store_true")
    run.set_defaults(func=cmd_run)

    replay = sub.add_parser("replay", help="replay a recorded run and compare the official result")
    replay.add_argument("run_id")
    replay.set_defaults(func=cmd_replay)

    scripts = sub.add_parser("scripts", help="list bundled deterministic scripts")
    scripts.set_defaults(func=cmd_scripts)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
