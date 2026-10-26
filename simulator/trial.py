"""Month-by-month trial: Meterless memory against no-memory Nemotron.

    python trial.py --out ../context/trials/monthly --model nvidia/nemotron-3.5-lightning-30b-a3b

Defaults match the best Feb–Apr 2024 bakeoff for integration: memory uses a fixed
21-session price brief and ``meterless/seeds/spread.md``; no-memory sees only the
current request. ``--days 30 --start 2024-01-16`` runs one calendar window instead
of months.

Each calendar month is its own run at the same starting cash. With ``--no-forget-notes``,
the memory arm updates the notebook after each month so month M only sees notes from
months before M. Historical prices and filings go through the analysis adapter
(``ANALYSIS_BACKEND=sqlite`` or ``zetaris``). Buy-and-hold is the benchmark. Results
append to ``results.jsonl``; a rerun skips months already recorded.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
DEFAULT_SEED = REPO / "meterless" / "seeds" / "spread.md"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO))

from agent.scripted import load_agent  # noqa: E402
from runner import LocalFeedReader, run_agent  # noqa: E402
from sim.service import SimulatorService  # noqa: E402

from meterless import notebook  # noqa: E402
from meterless.provider import TOKEN_SETTING, MeterlessRuntime, load_settings  # noqa: E402
from meterless.query import open_analysis_source  # noqa: E402
from meterless.trader import NemotronTrader  # noqa: E402

DATA_END = date(2026, 7, 21)
METRICS = ["run_id", "return_pct", "net_profit", "max_drawdown_pct", "sharpe", "fills", "rejections", "invalid_responses", "turnover"]
_write_lock = threading.Lock()


def one_window(start: str, days: int) -> list[tuple[str, str, str]]:
    """One evaluation window of ``days`` calendar days, inclusive of ``start``."""
    first = date.fromisoformat(start)
    end = min(first + timedelta(days=days - 1), DATA_END)
    return [(f"{first.isoformat()}+{days}d", first.isoformat(), end.isoformat())]


def months(first: str, last: str) -> list[tuple[str, str, str]]:
    y, m = map(int, first.split("-"))
    ly, lm = map(int, last.split("-"))
    out = []
    while (y, m) <= (ly, lm):
        end = min(date(y, m, calendar.monthrange(y, m)[1]), DATA_END)
        out.append((f"{y:04d}-{m:02d}", f"{y:04d}-{m:02d}-01", end.isoformat()))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def simulate(agent, cfg: dict) -> tuple[dict, SimulatorService]:
    service = SimulatorService()
    first = service.reset(cfg)
    feed = LocalFeedReader(service, first["run_id"])
    report = run_agent(agent, lambda _cfg: first, service.submit_action, feed, cfg, verbose=True)
    return report, service


def period_record(report: dict, bench: dict, service: SimulatorService, cfg: dict) -> dict:
    """Everything the notebook reviewer sees about a finished month."""
    run_dir = ROOT / "runs" / report["run_id"]
    feed = LocalFeedReader(service, report["run_id"])
    moves = {}
    for symbol in cfg["symbols"]:
        bars = [b for b in feed.prices(symbol, 40) if str(b["ts"])[:10] >= cfg["start"]]
        if bars:
            moves[symbol] = f"{bars[0]['open']:.2f} -> {bars[-1]['close']:.2f} ({(bars[-1]['close'] / bars[0]['open'] - 1) * 100:+.2f}%)"
    decisions = []
    for line in (run_dir / "actions.jsonl").read_text().splitlines():
        action = json.loads(line)
        payload, ack = action["payload"], action["ack"]
        orders = "; ".join(f"{o.get('side')} {o.get('quantity')} {o.get('symbol')}" for o in payload.get("orders") or []) or "hold"
        statuses = ",".join(o["status"] for o in ack.get("orders", []))
        decisions.append(f"{payload.get('window_start', '')[:10]} {orders}{' [' + statuses + ']' if statuses else ''} | {str(payload.get('reason', ''))[:160]}")
    fills = []
    pnl = {symbol: 0.0 for symbol in cfg["symbols"]}
    fills_path = run_dir / "fills.jsonl"
    if fills_path.exists():
        for line in fills_path.read_text().splitlines():
            f = json.loads(line)
            fills.append(f"{f['filled_at'][:10]} {f['side']} {f['quantity']} {f['symbol']} @ {f['fill_price']:.2f}")
            pnl[f["symbol"]] += f["notional"] if f["side"] == "SELL" else -f["notional"]
    for position in report.get("final_positions") or []:
        pnl[position["symbol"]] += position["market_value"]
    equity = [json.loads(line) for line in (run_dir / "equity.jsonl").read_text().splitlines()]
    invested = [1 - e["cash"] / e["equity"] for e in equity if e["equity"]]
    return {
        "period": f"{cfg['start']} to {cfg['end']}",
        "symbols": cfg["symbols"],
        "price_moves_open_to_close": moves,
        "agent_return_pct": round(report["return_pct"], 2),
        "buy_and_hold_return_pct": round(bench["return_pct"], 2),
        "max_drawdown_pct": round(report["max_drawdown_pct"], 2),
        "profit_by_symbol_usd": {k: round(v, 2) for k, v in pnl.items()},
        "average_invested_pct_of_equity": round(100 * sum(invested) / len(invested), 1) if invested else 0.0,
        "days_fully_in_cash": sum(1 for x in invested if x < 0.01),
        "decisions_lost_to_runtime_errors": sum(1 for d in decisions if "| meterless unavailable" in d),
        "fills": fills,
        "decisions": decisions,
    }


def _review_period(period: dict, queries) -> dict:
    """Short month record for the notebook. The full query log stays in queries.jsonl."""
    keep = (
        "period", "symbols", "price_moves_open_to_close", "agent_return_pct",
        "buy_and_hold_return_pct", "max_drawdown_pct", "profit_by_symbol_usd",
        "average_invested_pct_of_equity", "days_fully_in_cash",
        "decisions_lost_to_runtime_errors", "fills",
    )
    brief = {key: period[key] for key in keep}
    brief["queries"] = [
        {"query_id": item.query_id, "goal": item.goal, "error": item.error, "rows": item.text[:400]}
        for item in queries[-4:]
    ]
    return brief


def runtime_errors(report: dict) -> int:
    lines = (ROOT / "runs" / report["run_id"] / "actions.jsonl").read_text().splitlines()
    return sum(1 for line in lines if str(json.loads(line)["payload"].get("reason", "")).startswith("meterless unavailable"))


def record(out: Path, row: dict) -> None:
    with _write_lock, (out / "results.jsonl").open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    print(f"{row['arm']:7} {row['month']}  agent {row['return_pct']:+6.2f}%  b&h {row['bench_return_pct']:+6.2f}%  fills {row['fills']}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default="nvidia/nemotron-3.5-lightning-30b-a3b")
    parser.add_argument("--first", default="2024-01")
    parser.add_argument("--last", default="2026-07")
    parser.add_argument("--days", type=int, default=0, help="one window of this many calendar days, instead of calendar months")
    parser.add_argument("--start", default="2024-01-02", help="first calendar day when --days is set")
    parser.add_argument("--symbols", default="AAPL,MSFT,SPY")
    parser.add_argument("--cash", type=float, default=10000.0)
    parser.add_argument("--in-run-memory", type=int, default=5)
    parser.add_argument("--workers", type=int, default=6, help="parallel months for the nomem arm")
    parser.add_argument("--arms", default="memory,nomem")
    parser.add_argument("--retries", type=int, default=4, help="retries per model call after a runtime error")
    parser.add_argument("--thinking", default="off", choices=["on", "off"],
                        help="Nemotron reasoning. With it on, long prompts often run past Nightshift's ~150 s limit and return 504")
    parser.add_argument("--recall", default="brief", choices=["sql", "notes", "brief"],
                        help="memory arm: fixed 21-session price brief (default), model SQL, or notebook only")
    parser.add_argument("--seed-notes", default=str(DEFAULT_SEED),
                        help="notebook the memory arm starts with (default: meterless/seeds/spread.md)")
    parser.add_argument("--forget-notes", action=argparse.BooleanOptionalAction, default=True,
                        help="keep the seed notebook fixed across months (default). --no-forget-notes updates and carries it")
    parser.add_argument("--prompt-style", default="default", choices=["default", "deploy", "equal"],
                        help="trading prompt: default, deploy idle cash, or equal-weight entry when flat")
    args = parser.parse_args()
    if args.seed_notes in ("", "-"):
        args.seed_notes = ""

    out = Path(args.out).resolve()
    (out / "notebook").mkdir(parents=True, exist_ok=True)
    done = set()
    if (out / "results.jsonl").exists():
        done = {(r["arm"], r["month"]) for r in map(json.loads, (out / "results.jsonl").read_text().splitlines())}

    settings = load_settings()
    periods = one_window(args.start, args.days) if args.days else months(args.first, args.last)

    extra = {} if args.thinking == "on" else {"chat_template_kwargs": {"enable_thinking": False}}

    def runtime() -> MeterlessRuntime:
        return MeterlessRuntime(settings["base_url"], settings[TOKEN_SETTING], args.model, extra_body=extra)

    def config(month: str, start: str, end: str, arm: str) -> dict:
        return {"interval": "daily", "mode": "V1", "start": start, "end": end, "initial_cash": args.cash,
                "symbols": args.symbols.split(","), "label": f"trial-{arm}-{month}",
                "execution": {"max_position_pct_equity": 0}}

    bench_cache: dict[str, dict] = {}

    def bench(month: str, start: str, end: str) -> dict:
        with _write_lock:
            if month not in bench_cache:
                bench_cache[month], _ = simulate(load_agent("buy_and_hold", None), config(month, start, end, "bench"))
            return bench_cache[month]

    def row(arm: str, month: str, report: dict, b: dict, **extra) -> dict:
        return {"arm": arm, "month": month, "model": args.model, "thinking": args.thinking, **{k: report[k] for k in METRICS},
                "bench_return_pct": b["return_pct"], "excess_pct": report["return_pct"] - b["return_pct"],
                "runtime_errors": runtime_errors(report), **extra}

    def nomem_month(spec) -> None:
        month, start, end = spec
        cfg = config(month, start, end, "nomem")
        report, _ = simulate(NemotronTrader(runtime(), memory=0, retries=args.retries, prompt_style=args.prompt_style), cfg)
        record(out, row("nomem", month, report, bench(month, start, end), prompt_style=args.prompt_style))

    def memory_arm(analysis) -> None:
        seed_path = Path(args.seed_notes) if args.seed_notes else None
        notes = seed_path.read_text() if seed_path and seed_path.is_file() else ""
        for month, start, end in periods:
            path = out / "notebook" / f"{month}.md"
            if ("memory", month) in done:
                notes = path.read_text() if path.exists() else notes
                continue
            cfg = config(month, start, end, "memory")
            source = analysis if args.recall in ("sql", "brief") else None
            agent = NemotronTrader(
                runtime(),
                memory=args.in_run_memory,
                notes=notes,
                retries=args.retries,
                source=source,
                recall=args.recall,
                prompt_style=args.prompt_style,
            )
            report, service = simulate(agent, cfg)
            b = bench(month, start, end)
            period = period_record(report, b, service, cfg)
            with _write_lock, (out / "queries.jsonl").open("a") as fh:
                for item in agent.queries:
                    fh.write(json.dumps({"month": month, **item.as_dict()}) + "\n")
            if args.forget_notes:
                path.write_text(notes)
                revised = notes
            else:
                revised = notes
                for attempt in range(2):
                    try:
                        revised = notebook.update(runtime(), notes, _review_period(period, agent.queries))
                        break
                    except Exception as exc:
                        print(f"memory  {month}  notebook update attempt {attempt + 1} failed: {exc}", flush=True)
                        time.sleep(15 * (attempt + 1))
                path.write_text(revised)
            record(out, row(
                "memory", month, report, b,
                notebook_chars_used=len(notes),
                recall=args.recall,
                prompt_style=args.prompt_style,
                in_run_memory=args.in_run_memory,
            ))
            notes = revised

    print(f"recall {args.recall}  prompt {args.prompt_style}  in-run-memory {args.in_run_memory}", flush=True)
    arms = args.arms.split(",")
    analysis = open_analysis_source(snapshot_dir=ROOT / "sim" / "snapshot") if "memory" in arms and args.recall != "notes" else None
    threads = []
    if "memory" in arms:
        threads.append(threading.Thread(target=memory_arm, args=(analysis,)))
        threads[-1].start()
    if "nomem" in arms:
        todo = [m for m in periods if ("nomem", m[0]) not in done]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(nomem_month, todo))
    for t in threads:
        t.join()
    summarize(out)


def summarize(out: Path) -> None:
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    for arm in ("memory", "nomem"):
        rs = sorted((r for r in rows if r["arm"] == arm), key=lambda r: r["month"])
        if not rs:
            continue
        compound = 1.0
        for r in rs:
            compound *= 1 + r["return_pct"] / 100
        half = len(rs) // 2
        mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")  # noqa: E731
        print(f"{arm:7} months {len(rs):2}  mean {mean([r['return_pct'] for r in rs]):+.2f}%  "
              f"mean excess vs b&h {mean([r['excess_pct'] for r in rs]):+.2f}%  "
              f"first-half excess {mean([r['excess_pct'] for r in rs[:half]]):+.2f}%  "
              f"second-half excess {mean([r['excess_pct'] for r in rs[half:]]):+.2f}%  "
              f"compounded {(compound - 1) * 100:+.2f}%  beat b&h {sum(r['excess_pct'] > 0 for r in rs)}/{len(rs)}", flush=True)


if __name__ == "__main__":
    main()
