#!/usr/bin/env python3
"""Build a self-contained static dashboard from recorded trial outputs.

Reads a trial directory (results.jsonl or results.csv, queries.jsonl, notebooks,
and simulator run reports). Writes canonical extracts under dashboard/data/ and
a single dashboard/index.html. Missing optional logs are left empty or marked
"not recorded" — numbers are never invented.

  python scripts/build_dashboard.py \\
    --trial context/trials/mem-brief-seed-2024-02-04 \\
    --months 2024-04 \\
    --out dashboard
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "simulator" / "runs"
GROUND_REASON = re.compile(r"^decision follows SQL results", re.I)
SEED_DEFAULT = ROOT / "meterless" / "seeds" / "spread.md"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))


def _compound(returns: list[float]) -> float | None:
    if not returns:
        return None
    value = 1.0
    for r in returns:
        value *= 1.0 + r / 100.0
    return (value - 1.0) * 100.0


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _bench_profit(row: dict) -> float:
    cash = float(row.get("initial_cash") or 10_000)
    return cash * float(row.get("bench_return_pct") or 0) / 100.0


def load_results(trial: Path) -> list[dict]:
    csv_path = trial / "results.csv"
    jsonl_path = trial / "results.jsonl"
    if csv_path.exists():
        with csv_path.open() as fh:
            return list(csv.DictReader(fh))
    rows = _load_jsonl(jsonl_path)
    for row in rows:
        row.setdefault("initial_cash", 10_000)
        if "net_profit" not in row and "return_pct" in row:
            row["net_profit"] = float(row["initial_cash"]) * float(row["return_pct"]) / 100.0
    return rows


def filter_months(rows: list[dict], months: set[str] | None) -> list[dict]:
    if not months:
        return rows
    return [r for r in rows if str(r.get("month")) in months]


def export_results_csv(rows: list[dict], path: Path) -> None:
    fields = [
        "arm", "month", "run_id", "model", "thinking", "recall", "prompt_style",
        "return_pct", "net_profit", "bench_return_pct", "excess_pct",
        "max_drawdown_pct", "sharpe", "fills", "rejections", "invalid_responses",
        "turnover", "runtime_errors", "notebook_chars_used", "initial_cash",
        "tokens", "cost_usd",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {k: row.get(k) for k in fields}
            # Tokens and cost were not logged in these trials.
            out["tokens"] = row.get("tokens") if row.get("tokens") not in (None, "") else ""
            out["cost_usd"] = row.get("cost_usd") if row.get("cost_usd") not in (None, "") else ""
            writer.writerow(out)


def load_run(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    if not run_dir.is_dir():
        return {"run_id": run_id, "missing": True}
    report = json.loads((run_dir / "report.json").read_text()) if (run_dir / "report.json").exists() else {}
    config = json.loads((run_dir / "config.json").read_text()) if (run_dir / "config.json").exists() else {}
    requests = _load_jsonl(run_dir / "requests.jsonl")
    actions = _load_jsonl(run_dir / "actions.jsonl")
    fills = _load_jsonl(run_dir / "fills.jsonl")
    equity = _load_jsonl(run_dir / "equity.jsonl")
    by_req_fills: dict[str, list] = {}
    for fill in fills:
        by_req_fills.setdefault(str(fill.get("request_id")), []).append(fill)
    by_req_action = {str(a.get("request_id")): a for a in actions}
    by_req_request = {str(r.get("request_id")): r for r in requests}
    return {
        "run_id": run_id,
        "missing": False,
        "report": report,
        "config": config,
        "requests": requests,
        "actions": actions,
        "fills": fills,
        "equity": equity,
        "by_req_fills": by_req_fills,
        "by_req_action": by_req_action,
        "by_req_request": by_req_request,
    }


def notebook_text(trial: Path, month: str, seed_path: Path | None) -> dict[str, str]:
    path = trial / "notebook" / f"{month}.md"
    after = path.read_text() if path.exists() else ""
    before = seed_path.read_text() if seed_path and seed_path.is_file() else after
    # With --forget-notes the post-month file is the seed; still report both paths.
    return {"retrieved": before, "written": after, "path": str(path.relative_to(ROOT)) if path.exists() else ""}


def queries_for(queries: list[dict], month: str, request_id: str) -> list[dict]:
    prefix = f"{request_id}-"
    out = []
    for q in queries:
        if str(q.get("month")) != month:
            continue
        qid = str(q.get("query_id") or "")
        if qid.startswith(prefix) or request_id in qid:
            out.append({
                "query_id": q.get("query_id"),
                "goal": q.get("goal"),
                "sql": q.get("sql"),
                "as_of": q.get("as_of"),
                "error": q.get("error"),
                "elapsed_ms": q.get("elapsed_ms"),
                "backend": q.get("backend"),
                "columns": q.get("columns") or [],
                "rows_preview": (q.get("rows") or [])[:5],
                "row_count": len(q.get("rows") or []),
                "truncated": q.get("truncated"),
            })
    return out


def next_day_outcome(equity: list[dict], as_of: str | None) -> dict | None:
    if not equity or not as_of:
        return None
    stamps = [str(e.get("ts")) for e in equity]
    try:
        idx = stamps.index(as_of)
    except ValueError:
        # Match by date prefix if exact timestamp differs.
        day = as_of[:10]
        idx = next((i for i, t in enumerate(stamps) if t.startswith(day)), -1)
        if idx < 0:
            return None
    if idx + 1 >= len(equity):
        return {"note": "no later equity point in this run", "equity": equity[idx]}
    now, nxt = equity[idx], equity[idx + 1]
    return {
        "from_ts": now.get("ts"),
        "to_ts": nxt.get("ts"),
        "equity_from": now.get("equity"),
        "equity_to": nxt.get("equity"),
        "cash_from": now.get("cash"),
        "cash_to": nxt.get("cash"),
        "delta_equity": (None if now.get("equity") is None or nxt.get("equity") is None
                         else float(nxt["equity"]) - float(now["equity"])),
    }


def build_decisions(
    results: list[dict],
    queries: list[dict],
    trial: Path,
    seed_path: Path | None,
    runs_dir: Path,
) -> list[dict]:
    decisions = []
    for row in results:
        arm = str(row.get("arm"))
        month = str(row.get("month"))
        run_id = str(row.get("run_id") or "")
        if not run_id:
            continue
        run = load_run(run_id, runs_dir)
        notes = notebook_text(trial, month, seed_path)
        if run.get("missing"):
            decisions.append({
                "id": f"{arm}:{month}:missing",
                "arm": arm,
                "month": month,
                "run_id": run_id,
                "date": None,
                "request_id": None,
                "label": f"{arm} {month} (run files missing)",
                "missing_run": True,
            })
            continue
        for req in run["requests"]:
            rid = str(req.get("request_id"))
            action = run["by_req_action"].get(rid) or {}
            payload = action.get("payload") or {}
            reason = str(payload.get("reason") or "")
            as_of = str(req.get("as_of") or "")
            decisions.append({
                "id": f"{arm}:{month}:{rid}",
                "arm": arm,
                "month": month,
                "run_id": run_id,
                "date": as_of[:10] if as_of else None,
                "as_of": as_of,
                "request_id": rid,
                "step": req.get("step"),
                "label": f"{arm} · {month} · {as_of[:10] if as_of else rid} · {rid}",
                "missing_run": False,
                "account": req.get("account"),
                "notebook_retrieved": notes["retrieved"],
                "notebook_written": notes["written"],
                "notebook_path": notes["path"],
                "queries": queries_for(queries, month, rid),
                "decision": {
                    "orders": payload.get("orders") or [],
                    "reason": reason,
                    "reason_unsupported": bool(GROUND_REASON.match(reason)),
                    "accepted": (action.get("ack") or {}).get("accepted"),
                    "ack_orders": (action.get("ack") or {}).get("orders") or [],
                    "errors": (action.get("ack") or {}).get("errors") or [],
                },
                "order_repair": {
                    "recorded": False,
                    "note": "order_repairs.jsonl was not written for this trial; showing post-structure submitted orders only.",
                    "original_orders": None,
                    "repaired_orders": payload.get("orders") or [],
                },
                "fills": run["by_req_fills"].get(rid) or [],
                "next_day_outcome": next_day_outcome(run["equity"], as_of),
            })
    return decisions


def summarize(results: list[dict]) -> dict:
    by_arm: dict[str, list[dict]] = {}
    for row in results:
        by_arm.setdefault(str(row["arm"]), []).append(row)

    def arm_stats(arm: str) -> dict:
        rows = sorted(by_arm.get(arm, []), key=lambda r: str(r["month"]))
        if not rows:
            return {"arm": arm, "months": 0, "present": False}
        profits = [float(r["net_profit"]) for r in rows]
        returns = [float(r["return_pct"]) for r in rows]
        drawdowns = [float(r["max_drawdown_pct"]) for r in rows]
        sharpes = [float(r["sharpe"]) for r in rows if r.get("sharpe") not in (None, "")]
        beat = sum(1 for r in rows if float(r.get("excess_pct") or 0) > 0)
        tokens = [r.get("tokens") for r in rows if r.get("tokens") not in (None, "")]
        costs = [float(r["cost_usd"]) for r in rows if r.get("cost_usd") not in (None, "")]
        return {
            "arm": arm,
            "present": True,
            "months": len(rows),
            "mean_profit": _mean(profits),
            "twr_pct": _compound(returns),
            "max_drawdown_pct": max(drawdowns) if drawdowns else None,
            "mean_sharpe": _mean(sharpes),
            "months_beating_buy_and_hold": beat,
            "tokens": sum(float(t) for t in tokens) if tokens else None,
            "cost_usd": sum(costs) if costs else None,
            "tokens_recorded": bool(tokens),
            "cost_recorded": bool(costs),
        }

    months = sorted({str(r["month"]) for r in results})
    series = {"memory": [], "nomem": [], "best_rule": [], "buy_and_hold": [], "cash": []}
    for month in months:
        mem = next((r for r in results if r["arm"] == "memory" and str(r["month"]) == month), None)
        nom = next((r for r in results if r["arm"] == "nomem" and str(r["month"]) == month), None)
        rule = next((r for r in results if r["arm"] in ("rule", "best_rule") and str(r["month"]) == month), None)
        series["memory"].append(None if mem is None else float(mem["net_profit"]))
        series["nomem"].append(None if nom is None else float(nom["net_profit"]))
        series["best_rule"].append(None if rule is None else float(rule["net_profit"]))
        base = mem or nom or rule
        series["buy_and_hold"].append(None if base is None else _bench_profit(base))
        series["cash"].append(0.0)

    return {
        "months": months,
        "profit_series": series,
        "table": [arm_stats(a) for a in ("memory", "nomem", "rule", "best_rule")],
        "headline": _headline(by_arm),
    }


def _headline(by_arm: dict[str, list[dict]]) -> dict:
    mem = by_arm.get("memory") or []
    nom = by_arm.get("nomem") or []
    mem_profit = _mean([float(r["net_profit"]) for r in mem])
    nom_profit = _mean([float(r["net_profit"]) for r in nom])
    mem_twr = _compound([float(r["return_pct"]) for r in mem])
    nom_twr = _compound([float(r["return_pct"]) for r in nom])
    return {
        "memory_mean_profit": mem_profit,
        "nomem_mean_profit": nom_profit,
        "memory_twr_pct": mem_twr,
        "nomem_twr_pct": nom_twr,
        "memory_months": len(mem),
        "nomem_months": len(nom),
    }


def portfolio_series(results: list[dict], runs_dir: Path) -> dict[str, dict]:
    """Daily cash and mark-to-market value by symbol from recorded decision requests."""
    out: dict[str, dict] = {}
    for row in results:
        arm = str(row["arm"])
        run = load_run(str(row.get("run_id") or ""), runs_dir)
        if run.get("missing"):
            continue
        days = []
        symbols: set[str] = set()
        for req in run["requests"]:
            account = req.get("account") or {}
            day = str(req.get("as_of") or "")[:10]
            point = {"day": day, "cash": float(account.get("cash") or 0), "equity": float(account.get("equity") or 0)}
            for pos in account.get("positions") or []:
                sym = str(pos.get("symbol"))
                symbols.add(sym)
                mv = pos.get("market_value")
                if mv is None and pos.get("mark_price") is not None and pos.get("quantity") is not None:
                    mv = float(pos["mark_price"]) * float(pos["quantity"])
                point[sym] = float(mv or 0)
            days.append(point)
        # Fill missing symbols with 0 so Chart.js gets aligned series.
        for point in days:
            for sym in symbols:
                point.setdefault(sym, 0.0)
        bucket = out.setdefault(arm, {"days": [], "symbols": sorted(symbols)})
        # Prefer a single contiguous window; if multiple months, append chronologically.
        bucket["days"].extend(days)
        bucket["symbols"] = sorted(set(bucket["symbols"]) | symbols)
        for point in bucket["days"]:
            for sym in bucket["symbols"]:
                point.setdefault(sym, 0.0)
    for arm, payload in out.items():
        payload["days"] = sorted(payload["days"], key=lambda p: p["day"])
    return out


def learning_loop(trial: Path, results: list[dict], seed_path: Path | None) -> list[dict]:
    rounds = []
    months = sorted({str(r["month"]) for r in results if r.get("arm") == "memory"})
    for month in months:
        notes = notebook_text(trial, month, seed_path)
        text = notes["written"] or notes["retrieved"]
        rules = []
        current = None
        for line in text.splitlines():
            if re.match(r"^r\d+:", line):
                if current:
                    rules.append(current)
                current = {"id": line.split(":", 1)[0], "text": line.split(":", 1)[1].strip(), "evidence": []}
            elif line.startswith("evidence:") and current is not None:
                current["evidence"].append(line[len("evidence:"):].strip())
        if current:
            rules.append(current)
        mem = next((r for r in results if r["arm"] == "memory" and str(r["month"]) == month), None)
        rounds.append({
            "round": month,
            "notebook_path": notes["path"],
            "rules": rules,
            "track_record": None if mem is None else {
                "return_pct": float(mem["return_pct"]),
                "net_profit": float(mem["net_profit"]),
                "excess_pct": float(mem["excess_pct"]),
                "fills": int(mem.get("fills") or 0),
                "bench_return_pct": float(mem["bench_return_pct"]),
            },
        })
    return rounds


def derive_model_calls(decisions: list[dict], results: list[dict]) -> list[dict]:
    model_by_run = {str(r["run_id"]): r.get("model") for r in results}
    rows = []
    for d in decisions:
        if d.get("missing_run") or not d.get("request_id"):
            continue
        rows.append({
            "run_id": d["run_id"],
            "request_id": d["request_id"],
            "arm": d["arm"],
            "month": d["month"],
            "as_of": d.get("as_of"),
            "model": model_by_run.get(d["run_id"]),
            "reason": (d.get("decision") or {}).get("reason"),
            "reason_unsupported": (d.get("decision") or {}).get("reason_unsupported"),
            "orders": (d.get("decision") or {}).get("orders"),
            "prompt_tokens": None,
            "completion_tokens": None,
            "cost_usd": None,
            "source": "inferred_from_actions_jsonl",
            "note": "Token and cost fields are null; this trial did not log model_calls.jsonl.",
        })
    return rows


def derive_order_repairs(decisions: list[dict]) -> list[dict]:
    # Original pre-structure model JSON was not persisted. Keep the schema for later trials.
    return [{
        "run_id": d["run_id"],
        "request_id": d["request_id"],
        "arm": d["arm"],
        "month": d["month"],
        "original_orders": None,
        "repaired_orders": (d.get("decision") or {}).get("orders"),
        "recorded": False,
        "note": "No order_repairs.jsonl in this trial.",
    } for d in decisions if d.get("request_id") and not d.get("missing_run")]


def html_page(payload: dict) -> str:
    data = json.dumps(payload, default=str)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Quantagent</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=Newsreader:opsz,wght@6..72,500;6..72,600&display=swap" rel="stylesheet" />
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js" crossorigin="anonymous"></script>
<style>
:root {{
  color-scheme: light dark;
  --bg: #f3f1eb;
  --panel: #fffdf8;
  --ink: #12140f;
  --muted: #5f6458;
  --line: #d7d2c6;
  --accent: #1f6b4a;
  --accent-2: #1c4d8c;
  --warn: #9a5b12;
  --bad: #9b2c2c;
  --chip: #e4eee7;
  --hero: #0e1511;
  --hero-ink: #f4f1ea;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --sans: "Instrument Sans", "Segoe UI", sans-serif;
  --display: "Newsreader", Georgia, serif;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0d100e; --panel: #161b17; --ink: #eceae3; --muted: #9aa193;
    --line: #2a322c; --accent: #5dcaa5; --accent-2: #7eb0ea; --warn: #e0a45a;
    --bad: #f07178; --chip: #223028; --hero: #111712; --hero-ink: #f4f1ea;
  }}
}}
html[data-theme="light"] {{
  --bg: #f3f1eb; --panel: #fffdf8; --ink: #12140f; --muted: #5f6458;
  --line: #d7d2c6; --accent: #1f6b4a; --accent-2: #1c4d8c; --chip: #e4eee7;
  --hero: #0e1511; --hero-ink: #f4f1ea;
}}
html[data-theme="dark"] {{
  --bg: #0d100e; --panel: #161b17; --ink: #eceae3; --muted: #9aa193;
  --line: #2a322c; --accent: #5dcaa5; --accent-2: #7eb0ea; --chip: #223028;
  --hero: #111712; --hero-ink: #f4f1ea;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: var(--sans); background: var(--bg); color: var(--ink); line-height: 1.5; }}
.hero {{
  background: radial-gradient(1200px 500px at 10% -10%, #1f6b4a33, transparent 55%),
              radial-gradient(900px 420px at 90% 0%, #1c4d8c22, transparent 50%),
              var(--hero);
  color: var(--hero-ink); padding: 2.2rem 1.75rem 1.8rem; border-bottom: 1px solid #243028;
}}
.hero-top {{ display: flex; justify-content: space-between; gap: 1rem; align-items: start; }}
.brand {{ font-family: var(--display); font-size: clamp(2.2rem, 5vw, 3.4rem); margin: 0; letter-spacing: -0.03em; font-weight: 600; }}
.tag {{ margin: 0.55rem 0 0; max-width: 34rem; color: #c9cfc6; font-size: 1.05rem; }}
.hero-actions {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
main {{ padding: 1.25rem 1.75rem 3rem; max-width: 1180px; margin: 0 auto; }}
.stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.85rem; margin: -2rem auto 1.25rem; max-width: 1180px; padding: 0 1.75rem; position: relative; }}
@media (max-width: 900px) {{ .stats {{ grid-template-columns: 1fr 1fr; }} }}
.stat {{
  background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 1rem 1.05rem;
  box-shadow: 0 10px 30px #00000008;
}}
.stat .label {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; }}
.stat .value {{ font-family: var(--display); font-size: 1.55rem; margin-top: 0.25rem; letter-spacing: -0.02em; }}
.stat .sub {{ color: var(--muted); font-size: 0.82rem; margin-top: 0.2rem; }}
section {{
  background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
  padding: 1.15rem 1.25rem 1.3rem; margin-bottom: 1.1rem;
}}
h2 {{ margin: 0 0 0.35rem; font-size: 1.15rem; font-weight: 600; letter-spacing: -0.01em; }}
h3 {{ margin: 1rem 0 0.4rem; font-size: 0.95rem; }}
.lede {{ color: var(--muted); margin: 0 0 1rem; font-size: 0.92rem; }}
.grid {{ display: grid; gap: 1rem; }}
.grid.two {{ grid-template-columns: 1fr 1fr; }}
@media (max-width: 900px) {{ .grid.two {{ grid-template-columns: 1fr; }} }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
th, td {{ text-align: left; padding: 0.5rem 0.45rem; border-bottom: 1px solid var(--line); vertical-align: top; }}
th {{ color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; }}
.num {{ font-variant-numeric: tabular-nums; font-family: var(--mono); font-size: 0.86rem; }}
.chip {{
  display: inline-block; padding: 0.12rem 0.5rem; border-radius: 999px;
  background: var(--chip); color: var(--accent); font-size: 0.75rem; font-weight: 600;
}}
.chip.warn {{ color: var(--warn); }} .chip.bad {{ color: var(--bad); }}
button, select, input {{
  font: inherit; background: var(--panel); color: var(--ink);
  border: 1px solid var(--line); border-radius: 10px; padding: 0.5rem 0.75rem;
}}
button {{ cursor: pointer; }}
button.primary {{ background: var(--accent); color: #fff; border-color: transparent; font-weight: 600; }}
button.primary:hover {{ filter: brightness(1.05); }}
button.ghost {{ background: transparent; color: var(--hero-ink); border-color: #3a4640; }}
.hero button.ghost:hover {{ border-color: #8aa094; }}
pre, code {{ font-family: var(--mono); font-size: 0.8rem; }}
pre {{
  background: color-mix(in srgb, var(--bg) 70%, var(--panel));
  border: 1px solid var(--line); border-radius: 10px; padding: 0.75rem;
  overflow: auto; max-height: 220px; white-space: pre-wrap;
}}
.flow {{ display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; margin: 0.75rem 0; }}
.flow span {{
  border: 1px solid var(--line); background: color-mix(in srgb, var(--bg) 55%, var(--panel));
  border-radius: 8px; padding: 0.4rem 0.6rem; font-size: 0.8rem;
}}
.flow .arrow {{ border: 0; background: transparent; color: var(--muted); padding: 0 0.1rem; }}
ul.guarantees {{ margin: 0.35rem 0 0; padding-left: 1.1rem; color: var(--muted); }}
.chart-wrap {{ position: relative; height: 300px; }}
.decision-meta {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.5rem 0 0.75rem; }}
.empty {{ color: var(--muted); font-style: italic; }}
.launch {{
  display: grid; grid-template-columns: 1fr 1fr auto; gap: 0.65rem; align-items: end;
  margin-top: 0.85rem;
}}
@media (max-width: 700px) {{ .launch {{ grid-template-columns: 1fr; }} }}
.field label {{ display: block; font-size: 0.75rem; color: var(--muted); margin-bottom: 0.25rem; text-transform: uppercase; letter-spacing: 0.05em; }}
.field input {{ width: 100%; }}
#trialStatus {{ margin-top: 0.65rem; font-size: 0.88rem; color: var(--muted); min-height: 1.2em; }}
.rule-card {{
  border: 1px solid var(--line); border-radius: 12px; padding: 0.9rem 1rem; margin-bottom: 0.7rem;
  background: color-mix(in srgb, var(--bg) 45%, var(--panel));
}}
.rule-card .id {{ font-family: var(--mono); color: var(--accent); font-size: 0.8rem; font-weight: 600; }}
.rule-card .text {{ margin: 0.35rem 0 0.45rem; font-size: 1.02rem; }}
.rule-card .why {{ color: var(--muted); font-size: 0.88rem; }}
.flash-wrap {{
  display: grid; grid-template-columns: 56px 1fr 56px; gap: 0.65rem; align-items: stretch; margin-top: 0.75rem;
}}
@media (max-width: 700px) {{
  .flash-wrap {{ grid-template-columns: 44px 1fr 44px; }}
}}
.flash-nav {{
  display: flex; align-items: center; justify-content: center;
  border: 1px solid var(--line); border-radius: 14px; background: color-mix(in srgb, var(--bg) 50%, var(--panel));
  cursor: pointer; font-size: 1.6rem; line-height: 1; color: var(--ink); min-height: 220px;
}}
.flash-nav:hover {{ border-color: var(--accent); color: var(--accent); }}
.flash-nav:disabled {{ opacity: 0.35; cursor: default; border-color: var(--line); color: var(--muted); }}
.flashcard {{
  border: 1px solid var(--line); border-radius: 16px; padding: 1rem 1.1rem 1.15rem;
  background: var(--panel); min-height: 280px; position: relative; overflow: hidden;
}}
.flashcard.gain {{
  border-color: color-mix(in srgb, #1f6b4a 55%, var(--line));
  box-shadow: inset 4px 0 0 #1f6b4a;
  background: color-mix(in srgb, #1f6b4a 07%, var(--panel));
}}
.flashcard.loss {{
  border-color: color-mix(in srgb, #9b2c2c 55%, var(--line));
  box-shadow: inset 4px 0 0 #9b2c2c;
  background: color-mix(in srgb, #9b2c2c 07%, var(--panel));
}}
.flashcard.flat {{
  border-color: var(--line);
  box-shadow: inset 4px 0 0 #8a8578;
}}
.flash-head {{ display: flex; justify-content: space-between; gap: 0.75rem; align-items: start; flex-wrap: wrap; }}
.flash-title {{ font-family: var(--display); font-size: 1.25rem; margin: 0; letter-spacing: -0.02em; }}
.flash-delta {{ font-family: var(--mono); font-weight: 600; font-size: 1rem; }}
.flash-delta.gain {{ color: #1f6b4a; }}
.flash-delta.loss {{ color: #9b2c2c; }}
.flash-delta.flat {{ color: var(--muted); }}
.flash-counter {{ color: var(--muted); font-size: 0.82rem; margin: 0.35rem 0 0.75rem; }}
.flash-body h3 {{ margin-top: 0.85rem; }}
.flash-body {{ max-height: 420px; overflow: auto; }}
.order-boxes {{ display: flex; flex-wrap: wrap; gap: 0.45rem; margin: 0.35rem 0 0.15rem; }}
.order-box {{
  border: 1px solid var(--line); border-radius: 10px; padding: 0.45rem 0.65rem;
  background: color-mix(in srgb, var(--bg) 55%, var(--panel)); min-width: 4.5rem;
}}
.order-box .sym {{ font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }}
.order-box .qty {{ font-family: var(--mono); font-size: 1.05rem; font-weight: 600; margin-top: 0.15rem; }}
.order-box.buy .qty {{ color: #1f6b4a; }}
.order-box.sell .qty {{ color: #9b2c2c; }}
.order-box.hold {{ opacity: 0.85; }}
.deck-title {{ margin: 0 0 0.35rem; font-size: 1rem; }}
.deck-block + .deck-block {{ margin-top: 1.25rem; padding-top: 1.1rem; border-top: 1px solid var(--line); }}
</style>
</head>
<body>
<header class="hero">
  <div class="hero-top">
    <div>
      <h1 class="brand">Quantagent</h1>
      <p class="tag">Autonomous trading with Meterless memory — rules that compound, scores the market can verify.</p>
    </div>
    <div class="hero-actions">
      <button type="button" class="ghost" id="themeToggle">Theme</button>
    </div>
  </div>
  <div class="launch" id="launchForm">
    <div class="field">
      <label for="trialStart">Trial start</label>
      <input id="trialStart" type="date" value="2024-04-01" />
    </div>
    <div class="field">
      <label for="trialEnd">Trial end</label>
      <input id="trialEnd" type="date" value="2024-04-30" />
    </div>
    <button type="button" class="primary" id="launchTrial">Start new trial</button>
  </div>
  <div id="trialStatus"></div>
</header>

<div class="stats" id="heroStats"></div>

<main>
  <section>
    <h2>Performance</h2>
    <p class="lede">Net profit by month for memory, no-memory, buy-and-hold, and idle cash. Figures are from the simulator report only.</p>
    <div class="chart-wrap"><canvas id="profitChart"></canvas></div>
    <h3>Summary</h3>
    <div id="summaryTable"></div>
  </section>

  <section>
    <h2>Portfolio over time</h2>
    <p class="lede">Cash and each holding’s mark-to-market value by trading day. Memory and no-memory side by side.</p>
    <div class="grid two">
      <div>
        <h3>Memory</h3>
        <div class="chart-wrap"><canvas id="memPortfolio"></canvas></div>
      </div>
      <div>
        <h3>No-memory</h3>
        <div class="chart-wrap"><canvas id="nomemPortfolio"></canvas></div>
      </div>
    </div>
  </section>

  <section id="rules">
    <h2>Key memory rules</h2>
    <p class="lede">Rules the memory arm used, with the recorded evidence for why each exists.</p>
    <div id="rulesBody"></div>
  </section>

  <section id="trace">
    <h2>Decision flashcards</h2>
    <p class="lede">Separate decks for memory and no-memory. Green = next-day equity up; red = next-day equity down. Side panels move earlier or later in that deck.</p>

    <div class="deck-block" data-arm="memory">
      <h3 class="deck-title">Memory decisions</h3>
      <div class="flash-wrap">
        <button type="button" class="flash-nav" data-dir="-1" aria-label="Earlier memory decision">‹</button>
        <div class="flashcard flat" data-card><p class="empty">No memory decisions.</p></div>
        <button type="button" class="flash-nav" data-dir="1" aria-label="Later memory decision">›</button>
      </div>
    </div>

    <div class="deck-block" data-arm="nomem">
      <h3 class="deck-title">No-memory decisions</h3>
      <div class="flash-wrap">
        <button type="button" class="flash-nav" data-dir="-1" aria-label="Earlier no-memory decision">‹</button>
        <div class="flashcard flat" data-card><p class="empty">No no-memory decisions.</p></div>
        <button type="button" class="flash-nav" data-dir="1" aria-label="Later no-memory decision">›</button>
      </div>
    </div>
  </section>
</main>
<script id="dashboard-data" type="application/json">{data}</script>
<script>
const DATA = JSON.parse(document.getElementById('dashboard-data').textContent);
const ASSET_COLORS = {{
  cash: '#8a8578',
  AAPL: '#1f6b4a',
  MSFT: '#1c4d8c',
  SPY: '#9a5b12',
  equity: '#444',
}};

function money(v) {{
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const n = Number(v);
  return (n < 0 ? '-' : '') + '$' + Math.abs(n).toLocaleString(undefined, {{maximumFractionDigits: 2, minimumFractionDigits: 2}});
}}
function pct(v) {{
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const n = Number(v);
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}}
function theme() {{
  const s = getComputedStyle(document.documentElement);
  return {{
    ink: s.getPropertyValue('--ink').trim(),
    muted: s.getPropertyValue('--muted').trim(),
    line: s.getPropertyValue('--line').trim(),
  }};
}}
function esc(s) {{
  return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

const h = DATA.overview.headline || {{}};
document.getElementById('heroStats').innerHTML = `
  <div class="stat"><div class="label">Memory return</div><div class="value">${{pct(h.memory_twr_pct)}}</div><div class="sub">time-weighted</div></div>
  <div class="stat"><div class="label">No-memory return</div><div class="value">${{pct(h.nomem_twr_pct)}}</div><div class="sub">time-weighted</div></div>
  <div class="stat"><div class="label">Memory profit</div><div class="value">${{money(h.memory_mean_profit)}}</div><div class="sub">mean per month</div></div>
  <div class="stat"><div class="label">No-memory profit</div><div class="value">${{money(h.nomem_mean_profit)}}</div><div class="sub">mean per month</div></div>
`;

const months = DATA.overview.months;
const series = DATA.overview.profit_series;
const barColors = {{ memory: '#1f6b4a', nomem: '#1c4d8c', best_rule: '#9a5b12', buy_and_hold: '#6b6b6b', cash: '#a8a297' }};
const labels = {{ memory: 'Memory', nomem: 'No-memory', best_rule: 'Best rule', buy_and_hold: 'Buy & hold', cash: 'Cash' }};
const profitDatasets = Object.entries(series).map(([key, values]) => {{
  if (!values.some(v => v !== null && v !== undefined)) return null;
  return {{ label: labels[key] || key, data: values, backgroundColor: barColors[key] || '#888' }};
}}).filter(Boolean);

const axisOpts = (xLabel, yLabel, extraX = {{}}, extraY = {{}}) => ({{
  x: {{
    title: {{ display: true, text: xLabel, color: theme().muted, font: {{ size: 12, weight: '500' }} }},
    ticks: {{ color: theme().muted }},
    grid: {{ color: theme().line }},
    ...extraX,
  }},
  y: {{
    title: {{ display: true, text: yLabel, color: theme().muted, font: {{ size: 12, weight: '500' }} }},
    ticks: {{ color: theme().muted }},
    grid: {{ color: theme().line }},
    ...extraY,
  }},
}});

const profitChart = new Chart(document.getElementById('profitChart'), {{
  type: 'bar',
  data: {{ labels: months, datasets: profitDatasets }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    layout: {{ padding: {{ top: 4, bottom: 2 }} }},
    scales: axisOpts('Month', 'Net profit ($)'),
    plugins: {{ legend: {{ labels: {{ color: theme().ink }} }} }},
  }},
}});

function portfolioChart(canvasId, arm) {{
  const pack = (DATA.portfolio || {{}})[arm];
  if (!pack || !pack.days.length) {{
    const el = document.getElementById(canvasId);
    el.parentElement.insertAdjacentHTML('beforeend', '<p class="empty">No portfolio series recorded.</p>');
    return null;
  }}
  const days = pack.days.map(p => p.day);
  const keys = ['cash', ...(pack.symbols || [])];
  const datasets = keys.map(k => ({{
    label: k === 'cash' ? 'Cash' : k,
    data: pack.days.map(p => p[k] ?? 0),
    borderColor: ASSET_COLORS[k] || '#777',
    backgroundColor: 'transparent',
    tension: 0.15,
    pointRadius: 0,
    borderWidth: 2,
  }}));
  return new Chart(document.getElementById(canvasId), {{
    type: 'line',
    data: {{ labels: days, datasets }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      layout: {{ padding: {{ top: 4, bottom: 2 }} }},
      interaction: {{ mode: 'index', intersect: false }},
      scales: axisOpts(
        'Trading day',
        'Value ($)',
        {{ ticks: {{ color: theme().muted, maxRotation: 0, autoSkipPadding: 12 }}, grid: {{ display: false }} }},
      ),
      plugins: {{ legend: {{ labels: {{ color: theme().ink }} }} }},
    }},
  }});
}}
const memChart = portfolioChart('memPortfolio', 'memory');
const nomemChart = portfolioChart('nomemPortfolio', 'nomem');

function renderSummary() {{
  const rows = DATA.overview.table.filter(r => r.present);
  if (!rows.length) {{
    document.getElementById('summaryTable').innerHTML = '<p class="empty">No summary rows.</p>';
    return;
  }}
  document.getElementById('summaryTable').innerHTML = `<table>
    <thead><tr>
      <th>Arm</th><th>Mean profit</th><th>TWR</th><th>Max DD</th><th>Sharpe</th><th>Beat b&amp;h</th>
    </tr></thead>
    <tbody>${{rows.map(r => `<tr>
      <td>${{esc(labels[r.arm] || r.arm)}}</td>
      <td class="num">${{money(r.mean_profit)}}</td>
      <td class="num">${{pct(r.twr_pct)}}</td>
      <td class="num">${{pct(r.max_drawdown_pct)}}</td>
      <td class="num">${{r.mean_sharpe == null ? '—' : Number(r.mean_sharpe).toFixed(2)}}</td>
      <td class="num">${{r.months_beating_buy_and_hold}}/${{r.months}}</td>
    </tr>`).join('')}}</tbody></table>`;
}}
renderSummary();

function renderRules() {{
  const rounds = DATA.learning || [];
  const seen = new Map();
  rounds.forEach(r => (r.rules || []).forEach(rule => {{
    const key = rule.id + '|' + rule.text;
    if (!seen.has(key)) seen.set(key, {{...rule, rounds: [r.round], track: r.track_record}});
    else seen.get(key).rounds.push(r.round);
  }}));
  const items = [...seen.values()];
  if (!items.length) {{
    document.getElementById('rulesBody').innerHTML = '<p class="empty">No memory rules recorded.</p>';
    return;
  }}
  document.getElementById('rulesBody').innerHTML = items.map(rule => `
    <div class="rule-card">
      <div class="id">${{esc(rule.id)}} · rounds ${{esc((rule.rounds || []).join(', '))}}</div>
      <div class="text">${{esc(rule.text)}}</div>
      <div class="why"><strong>Why:</strong> ${{esc((rule.evidence || []).join(' ') || 'No evidence line recorded.')}}</div>
      ${{rule.track ? `<div class="why" style="margin-top:0.35rem">Latest track: ${{pct(rule.track.return_pct)}} return, ${{money(rule.track.net_profit)}} profit, ${{pct(rule.track.excess_pct)}} vs buy-and-hold.</div>` : ''}}
    </div>`).join('');
}}
renderRules();

const decks = {{
  memory: {{
    items: DATA.decisions.filter(d => d.arm === 'memory'),
    index: 0,
    root: document.querySelector('.deck-block[data-arm="memory"]'),
  }},
  nomem: {{
    items: DATA.decisions.filter(d => d.arm === 'nomem'),
    index: 0,
    root: document.querySelector('.deck-block[data-arm="nomem"]'),
  }},
}};

function decisionTone(d) {{
  const delta = d && d.next_day_outcome ? d.next_day_outcome.delta_equity : null;
  if (delta === null || delta === undefined || Number.isNaN(Number(delta))) return 'flat';
  if (Number(delta) > 0) return 'gain';
  if (Number(delta) < 0) return 'loss';
  return 'flat';
}}

function orderBoxes(orders) {{
  if (!orders || !orders.length) {{
    return `<div class="order-boxes"><div class="order-box hold"><div class="sym">Decision</div><div class="qty">hold</div></div></div>`;
  }}
  return `<div class="order-boxes">${{orders.map(o => {{
    const side = String(o.side || '').toLowerCase();
    const sign = side === 'sell' ? '−' : side === 'buy' ? '+' : '';
    return `<div class="order-box ${{esc(side)}}">
      <div class="sym">${{esc(o.symbol || '?')}} · ${{esc(o.side || '')}}</div>
      <div class="qty">${{sign}}${{esc(o.quantity)}}</div>
    </div>`;
  }}).join('')}}</div>`;
}}

function renderDeck(arm) {{
  const deck = decks[arm];
  if (!deck || !deck.root) return;
  const card = deck.root.querySelector('[data-card]');
  const prev = deck.root.querySelector('.flash-nav[data-dir="-1"]');
  const next = deck.root.querySelector('.flash-nav[data-dir="1"]');
  const total = deck.items.length;
  if (!total) {{
    card.className = 'flashcard flat';
    card.innerHTML = `<p class="empty">No ${{arm === 'memory' ? 'memory' : 'no-memory'}} decisions.</p>`;
    prev.disabled = true; next.disabled = true;
    return;
  }}
  deck.index = Math.max(0, Math.min(total - 1, deck.index));
  prev.disabled = deck.index <= 0;
  next.disabled = deck.index >= total - 1;
  const d = deck.items[deck.index];
  if (!d || d.missing_run) {{
    card.className = 'flashcard flat';
    card.innerHTML = '<p class="empty">Run files were not found for this decision.</p>';
    return;
  }}
  const tone = decisionTone(d);
  const delta = d.next_day_outcome ? d.next_day_outcome.delta_equity : null;
  card.className = 'flashcard ' + tone;
  const reasonFlag = d.decision.reason_unsupported
    ? '<span class="chip warn">reason unsupported</span>'
    : '<span class="chip">reason ok</span>';
  const queries = (d.queries || []).map(q => `
    <div style="margin:0.55rem 0">
      <div><strong>${{esc(q.query_id)}}</strong> · ${{esc(q.goal || '')}}
        ${{q.error ? '<span class="chip bad">error</span>' : ''}}
      </div>
      <pre>${{esc(q.sql || '')}}</pre>
      ${{q.error ? '<pre>' + esc(q.error) + '</pre>' : ''}}
      <div class="muted">${{q.row_count || 0}} rows · first ${{(q.rows_preview || []).length}}</div>
      <pre>${{esc(JSON.stringify(q.rows_preview || [], null, 2))}}</pre>
    </div>`).join('') || '<p class="empty">No SQL for this decision.</p>';

  const contextBlock = arm === 'memory'
    ? `<h3>Rules in context</h3><pre>${{esc(d.notebook_retrieved || '(empty)')}}</pre>`
    : '';

  card.innerHTML = `
    <div class="flash-head">
      <div>
        <h3 class="flash-title">${{esc((d.as_of || '').slice(0,10))}}</h3>
        <div class="decision-meta" style="margin-top:0.35rem">
          <span class="chip">${{esc(d.request_id || '')}}</span>
          ${{reasonFlag}}
        </div>
      </div>
      <div class="flash-delta ${{tone}}">${{delta == null ? '—' : money(delta)}} <span style="font-weight:500;font-size:0.8rem">next day</span></div>
    </div>
    <div class="flash-counter">${{deck.index + 1}} / ${{total}}</div>
    <h3>Decision</h3>
    ${{orderBoxes(d.decision.orders || [])}}
    <div class="flash-body">
      <h3>Reason</h3>
      <pre>${{esc(d.decision.reason || '')}}</pre>
      ${{contextBlock}}
      ${{arm === 'memory' ? `<h3>SQL</h3>${{queries}}` : ''}}
      <h3>Fills</h3>
      <pre>${{esc(JSON.stringify(d.fills || [], null, 2))}}</pre>
      <h3>Next day</h3>
      <pre>${{esc(JSON.stringify(d.next_day_outcome, null, 2))}}</pre>
    </div>
  `;
}}

Object.keys(decks).forEach(arm => {{
  const deck = decks[arm];
  if (!deck.root) return;
  deck.root.querySelectorAll('.flash-nav').forEach(btn => {{
    btn.addEventListener('click', () => {{
      deck.index += Number(btn.getAttribute('data-dir'));
      renderDeck(arm);
    }});
  }});
  renderDeck(arm);
}});

document.getElementById('themeToggle').addEventListener('click', () => {{
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : cur === 'light' ? 'dark'
    : (matchMedia('(prefers-color-scheme: dark)').matches ? 'light' : 'dark');
  document.documentElement.setAttribute('data-theme', next);
  profitChart.update();
  if (memChart) memChart.update();
  if (nomemChart) nomemChart.update();
}});

document.getElementById('launchTrial').addEventListener('click', async () => {{
  const start = document.getElementById('trialStart').value;
  const end = document.getElementById('trialEnd').value;
  const status = document.getElementById('trialStatus');
  if (!start || !end) {{ status.textContent = 'Choose a start and end date.'; return; }}
  if (end < start) {{ status.textContent = 'End date must be on or after start.'; return; }}
  status.textContent = 'Launching trial…';
  try {{
    const res = await fetch('/api/trial', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ start, end }}),
    }});
    const body = await res.json().catch(() => ({{}}));
    if (!res.ok) {{
      status.textContent = body.error || ('Launch failed (' + res.status + '). Use scripts/serve_dashboard.py on :8080.');
      return;
    }}
    status.textContent = body.message || ('Trial started' + (body.out ? (' · ' + body.out) : ''));
  }} catch (err) {{
    status.textContent = 'Could not reach /api/trial. Serve with: python scripts/serve_dashboard.py';
  }}
}});
</script>
</body>
</html>
"""

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, required=True, help="trial output directory")
    parser.add_argument("--months", default="", help="comma-separated months to include, e.g. 2024-04")
    parser.add_argument("--out", type=Path, default=ROOT / "dashboard", help="output directory")
    parser.add_argument("--seed-notes", type=Path, default=SEED_DEFAULT, help="seed notebook used as retrieved rules")
    parser.add_argument("--runs-dir", type=Path, default=RUNS, help="simulator runs directory")
    args = parser.parse_args()

    runs_dir = args.runs_dir.resolve()
    trial = args.trial.resolve()
    if not trial.is_dir():
        raise SystemExit(f"trial directory not found: {trial}")

    months = {m.strip() for m in args.months.split(",") if m.strip()} or None
    results = filter_months(load_results(trial), months)
    if not results:
        raise SystemExit("no result rows after filters")

    queries = filter_months(_load_jsonl(trial / "queries.jsonl"), months)
    seed = args.seed_notes if args.seed_notes and args.seed_notes.is_file() else None

    out = args.out.resolve()
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    export_results_csv(results, data_dir / "results.csv")
    _write_jsonl(data_dir / "queries.jsonl", queries)

    decisions = build_decisions(results, queries, trial, seed, runs_dir)
    model_calls = derive_model_calls(decisions, results)
    repairs = derive_order_repairs(decisions)
    _write_jsonl(data_dir / "model_calls.jsonl", model_calls)
    _write_jsonl(data_dir / "order_repairs.jsonl", repairs)
    _write_jsonl(data_dir / "decisions.jsonl", decisions)

    overview = summarize(results)
    learning = learning_loop(trial, results, seed)
    portfolio = portfolio_series(results, runs_dir)

    payload = {
        "meta": {
            "product": "Quantagent",
            "months_filter": sorted(months) if months else None,
        },
        "overview": overview,
        "portfolio": portfolio,
        "decisions": decisions,
        "learning": learning,
    }
    (data_dir / "dashboard.json").write_text(json.dumps(payload, indent=2, default=str))
    (out / "index.html").write_text(html_page(payload))
    print(f"wrote {out / 'index.html'}")
    print(f"wrote {data_dir / 'results.csv'} ({len(results)} rows)")
    print(f"wrote {data_dir / 'queries.jsonl'} ({len(queries)} rows)")
    print(f"wrote {data_dir / 'model_calls.jsonl'} ({len(model_calls)} rows; tokens not recorded)")
    print(f"wrote {data_dir / 'order_repairs.jsonl'} ({len(repairs)} rows; originals not recorded)")
    print(f"decisions {len(decisions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
