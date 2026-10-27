"""Structured notebook written after each explore round.

Every number comes from explore runs inside the explore window, or from test
months that finished before the current test month. Nothing here calls a model.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import timedelta
from pathlib import Path
from typing import Any

_TREND_BAND_PCT = 2.0
_VOL_BANDS_PCT = (15.0, 25.0)
_HIGH_T = 2.5
_MEDIUM_OOS_T = 1.5
_HIGH_OOS_MONTHS = 3
_TOP = 3


def rule_params(spec: dict) -> dict:
    """The fields that decide what a rule trades. Names and ids are left out."""
    signal = spec.get("signal") or {}
    selection = spec.get("selection") or {}
    return {
        "signal": signal.get("kind"),
        "lookback": signal.get("lookback"),
        "max_days_since_filing": signal.get("max_days_since_filing"),
        "selection": selection.get("mode"),
        "top_n": selection.get("top_n") if selection.get("mode") != "threshold" else None,
        "threshold": selection.get("threshold") if selection.get("mode") == "threshold" else None,
        "target_weight": spec.get("target_weight"),
        "cash_floor": spec.get("cash_floor"),
        "rebalance_every": spec.get("rebalance_every"),
        "min_weight_change": spec.get("min_weight_change"),
        "drawdown_cut": spec.get("drawdown_cut"),
        "universe": sorted(spec.get("universe") or []),
    }


def rule_signature(spec: dict) -> str:
    return json.dumps(rule_params(spec), sort_keys=True)


def describe_params(params: dict) -> str:
    pick = f"top {params['top_n']}" if params.get("selection") != "threshold" else f"score >= {params.get('threshold')}"
    filing = "" if params.get("max_days_since_filing") is None else f", only within {params['max_days_since_filing']} days of a filing"
    return (
        f"{params['signal']} lookback {params['lookback']}, {pick}{filing}, "
        f"{_pct(params['target_weight'])} each, cash floor {_pct(params['cash_floor'])}, "
        f"rebalance every {params['rebalance_every']} sessions, min weight change {_pct(params['min_weight_change'])}, "
        f"drawdown cut {_pct(params['drawdown_cut'])}, universe {','.join(params['universe'])}"
    )


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.0f}%"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def daily_factors(run_dir: Path) -> list[float]:
    """Deposit-free daily growth: (equity_t - deposit_t) / equity_(t-1)."""
    equity = read_jsonl(run_dir / "equity.jsonl")
    deposits: dict[str, float] = {}
    for event in read_jsonl(run_dir / "cash_events.jsonl"):
        if event.get("kind") == "deposit":
            key = str(event["at"]).replace("+00:00", "Z")
            deposits[key] = deposits.get(key, 0.0) + float(event["amount"])
    out = []
    for previous, current in zip(equity, equity[1:]):
        before = float(previous["equity"])
        if before <= 0:
            continue
        out.append((float(current["equity"]) - deposits.get(str(current["ts"]), 0.0)) / before)
    return out


def trade_fingerprint(run_dir: Path) -> str | None:
    """Rules with the same fills behaved as one rule. None when the run left no fill log."""
    path = run_dir / "fills.jsonl"
    if not path.exists():
        return None
    fills = [(f.get("filled_at"), f.get("side"), f.get("symbol"), f.get("quantity")) for f in read_jsonl(path)]
    return json.dumps(fills)


def average_invested(run_dir: Path) -> float | None:
    rows = [row for row in read_jsonl(run_dir / "equity.jsonl") if float(row.get("equity") or 0) > 0]
    if not rows:
        return None
    return sum(1 - float(row["cash"]) / float(row["equity"]) for row in rows) / len(rows)


def excess_t(rule_dir: Path, bench_dir: Path) -> tuple[float | None, float | None, int]:
    """Mean daily excess growth over buy-and-hold (percent) and its t statistic."""
    left, right = daily_factors(rule_dir), daily_factors(bench_dir)
    diffs = [a - b for a, b in zip(left, right)]
    if len(diffs) < 2:
        return None, None, len(diffs)
    mean = statistics.fmean(diffs)
    spread = statistics.stdev(diffs)
    if spread == 0:
        return mean * 100, None, len(diffs)
    return mean * 100, mean / (spread / math.sqrt(len(diffs))), len(diffs)


def market_conditions(store: Any, symbols: list[str], start: str, end: str) -> dict:
    """Trend and volatility of each symbol inside the explore window, from daily closes."""
    import pandas as pd

    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end, tz="UTC") + timedelta(days=1)
    frame = store.bars("daily", symbols, lo, hi)
    per_symbol: dict[str, dict] = {}
    for symbol in symbols:
        closes = [float(value) for value in frame.loc[frame.symbol == symbol].sort_values("ts")["close"]]
        if len(closes) < 2:
            continue
        rets = [b / a - 1 for a, b in zip(closes, closes[1:])]
        peak, worst = closes[0], 0.0
        for close in closes:
            peak = max(peak, close)
            worst = max(worst, 1 - close / peak)
        per_symbol[symbol] = {
            "return_pct": round((closes[-1] / closes[0] - 1) * 100, 2),
            "ann_vol_pct": round(statistics.stdev(rets) * math.sqrt(252) * 100, 1) if len(rets) > 1 else None,
            "max_drawdown_pct": round(worst * 100, 2),
            "sessions": len(closes),
        }
    if not per_symbol:
        return {"symbols": {}, "trend": "unknown", "volatility": "unknown", "equal_weight_return_pct": None, "mean_ann_vol_pct": None}
    mean_ret = statistics.fmean(item["return_pct"] for item in per_symbol.values())
    vols = [item["ann_vol_pct"] for item in per_symbol.values() if item["ann_vol_pct"] is not None]
    mean_vol = statistics.fmean(vols) if vols else None
    trend = "up" if mean_ret > _TREND_BAND_PCT else "down" if mean_ret < -_TREND_BAND_PCT else "flat"
    if mean_vol is None:
        level = "unknown"
    else:
        level = "low" if mean_vol < _VOL_BANDS_PCT[0] else "high" if mean_vol > _VOL_BANDS_PCT[1] else "normal"
    return {
        "symbols": per_symbol,
        "trend": trend,
        "volatility": level,
        "equal_weight_return_pct": round(mean_ret, 2),
        "mean_ann_vol_pct": None if mean_vol is None else round(mean_vol, 1),
    }


def cash_return_pct(sessions: int, annual_rate: float) -> float:
    return ((1 + annual_rate / 252) ** sessions - 1) * 100


def confidence(t_stat: float | None, sessions: int, n_specs: int, oos: list[dict]) -> dict:
    """high, medium or low, with the rule that produced it.

    The best of many rules looks good by luck, so explore alone needs t >= 2.5.
    """
    wins = sum(1 for item in oos if (item.get("excess_pp") or 0) > 0)
    t = -math.inf if t_stat is None else t_stat
    if t >= _HIGH_T and len(oos) >= _HIGH_OOS_MONTHS and wins * 3 >= 2 * len(oos):
        level = "high"
    elif t >= _HIGH_T or (t >= _MEDIUM_OOS_T and len(oos) >= 2 and wins == len(oos)):
        level = "medium"
    else:
        level = "low"
    t_text = "n/a" if t_stat is None else f"{t_stat:.2f}"
    basis = (
        f"{sessions} explore sessions, {n_specs} rules tried, daily excess t = {t_text}, "
        f"{len(oos)} finished out-of-sample months ({wins} beat buy-and-hold). "
        f"high needs t >= {_HIGH_T} and >= {_HIGH_OOS_MONTHS} out-of-sample months with 2/3 beating buy-and-hold; "
        f"medium needs t >= {_HIGH_T}, or t >= {_MEDIUM_OOS_T} with every out-of-sample month (at least 2) beating buy-and-hold."
    )
    return {"level": level, "t_stat": t_stat, "sessions": sessions, "n_specs": n_specs,
            "oos_months": len(oos), "oos_wins": wins, "basis": basis}


def _why_worst(row: dict, bench: dict, market: dict, median_turnover: float, invested_bench: float | None) -> list[str]:
    params = row["params"]
    reasons = [f"excess vs buy-and-hold {row['excess_pp']:+.2f} pp (TWR {row['twr']:.2f}% vs {bench['twr']:.2f}%)"]
    invested = row.get("avg_invested_pct")
    if invested is not None and invested_bench is not None and invested < invested_bench * 100 - 25:
        reasons.append(f"averaged {invested:.0f}% invested vs buy-and-hold {invested_bench * 100:.0f}%, so it missed most of the move")
    if median_turnover and (row.get("turnover") or 0) > 1.5 * median_turnover:
        reasons.append(f"turnover {row['turnover']:.2f} vs median {median_turnover:.2f}, paying slippage on each trade")
    if (row.get("max_drawdown") or 0) > (bench.get("max_drawdown") or 0) + 0.5:
        reasons.append(f"drawdown {row['max_drawdown']:.2f}% vs buy-and-hold {bench['max_drawdown']:.2f}%")
    trend = market.get("trend")
    if params["signal"] == "mean_reversion" and trend == "up":
        reasons.append(f"bet on reversals while the universe trended up {market['equal_weight_return_pct']:+.2f}%")
    if params["signal"] == "momentum" and trend in ("flat", "down"):
        reasons.append(f"followed trends in a {trend} window ({market['equal_weight_return_pct']:+.2f}%)")
    if params["signal"] == "volatility":
        reasons.append("ranked by low volatility, which ignores direction")
    if (params.get("cash_floor") or 0) >= 0.3 and trend == "up":
        reasons.append(f"kept a {_pct(params['cash_floor'])} cash floor while stocks rose")
    return reasons


def build_notebook(
    *,
    notebook_id: str,
    parent_id: str | None,
    round_id: str,
    explore_start: str,
    explore_end: str,
    test_start: str,
    sessions: int,
    rows: list[dict],
    bench: dict,
    specs: dict[str, dict],
    market: dict,
    track: list[dict],
    runs_dir: Path,
    cash_annual_rate: float,
) -> dict:
    """`rows` are finished explore rules. `track` is every out-of-sample record so far."""
    finished = [row for row in rows if row.get("net_profit") is not None and row.get("spec_id") in specs]
    finished.sort(key=lambda row: (row["net_profit"], row.get("twr") or -1e9), reverse=True)
    tested = len(finished)
    aliases: dict[str, list[str]] = {}
    distinct: list[dict] = []
    seen: dict[str, str] = {}
    for row in finished:
        key = trade_fingerprint(runs_dir / str(row.get("run_id") or "")) or f"spec:{row['spec_id']}"
        if key in seen:
            aliases[seen[key]].append(specs[row["spec_id"]].get("name"))
            continue
        seen[key] = row["spec_id"]
        aliases[row["spec_id"]] = []
        distinct.append(row)
    finished = distinct
    past = [item for item in track if item["test_end"] < test_start]
    bench_dir = runs_dir / str(bench.get("run_id") or "")
    invested_bench = average_invested(bench_dir)
    turnovers = [float(row.get("turnover") or 0) for row in finished]
    median_turnover = statistics.median(turnovers) if turnovers else 0.0

    def entry(rank: int, row: dict) -> dict:
        spec = specs[row["spec_id"]]
        params = rule_params(spec)
        signature = rule_signature(spec)
        mean_pct, t_stat, n = excess_t(runs_dir / row["run_id"], bench_dir)
        invested = average_invested(runs_dir / row["run_id"])
        oos = [item for item in past if item["signature"] == signature]
        return {
            "rank": rank,
            "name": spec.get("name"),
            "spec_id": row["spec_id"],
            "params": params,
            "spec": spec,
            "net_profit": round(row["net_profit"], 2),
            "twr": round(row.get("twr") or 0.0, 2),
            "excess_pp": round((row.get("twr") or 0.0) - (bench.get("twr") or 0.0), 2),
            "max_drawdown": round(row.get("max_drawdown") or 0.0, 2),
            "turnover": round(row.get("turnover") or 0.0, 2),
            "sharpe": None if row.get("sharpe") is None else round(row["sharpe"], 2),
            "avg_invested_pct": None if invested is None else round(invested * 100, 1),
            "daily_excess_mean_pct": None if mean_pct is None else round(mean_pct, 4),
            "daily_excess_t": None if t_stat is None else round(t_stat, 2),
            "daily_excess_n": n,
            "same_trades_as": aliases.get(row["spec_id"], []),
            "oos": oos,
            "confidence": confidence(t_stat, sessions, tested, oos),
        }

    top = [entry(index + 1, row) for index, row in enumerate(finished[:_TOP])]
    bottom_rows = finished[_TOP:][-_TOP:] if len(finished) > _TOP else []
    worst = []
    for row in reversed(bottom_rows):
        item = entry(finished.index(row) + 1, row)
        item["why"] = _why_worst(item, bench, market, median_turnover, invested_bench)
        worst.append(item)

    cash_pct = cash_return_pct(sessions, cash_annual_rate)
    if top and top[0]["excess_pp"] > 0 and top[0]["twr"] > cash_pct:
        lead = top[0]
        action = {"action": "apply_rule", "rule": lead["name"], "spec_id": lead["spec_id"], "confidence": lead["confidence"]}
        backup = top[1]["name"] if len(top) > 1 else None
        if lead["confidence"]["level"] == "low":
            action["instruction"] = (
                f"Apply {lead['name']} at its own weights and no larger. "
                + (f"Use {backup} only on a day {lead['name']} has no signal (null features). " if backup else "")
                + "The explore edge is not separable from luck at this sample size, so do not add to it."
            )
        else:
            action["instruction"] = f"Apply {lead['name']} as written."
    elif (bench.get("twr") or 0) > cash_pct:
        action = {"action": "buy_and_hold", "rule": "buy_and_hold", "confidence": confidence(None, sessions, tested, []),
                  "instruction": "No explore rule beat buy-and-hold. Hold the universe in roughly equal weights."}
    else:
        action = {"action": "cash", "rule": "cash", "confidence": confidence(None, sessions, tested, []),
                  "instruction": f"Buy-and-hold ({bench.get('twr', 0):.2f}%) did not beat cash ({cash_pct:.2f}%). Stay in cash."}

    return {
        "notebook_id": notebook_id,
        "parent_id": parent_id,
        "round": round_id,
        "explore_start": explore_start,
        "explore_end": explore_end,
        "sessions": sessions,
        "n_specs": tested,
        "n_distinct": len(finished),
        "market": market,
        "buy_and_hold": {"twr": round(bench.get("twr") or 0.0, 2), "max_drawdown": round(bench.get("max_drawdown") or 0.0, 2),
                          "avg_invested_pct": None if invested_bench is None else round(invested_bench * 100, 1)},
        "cash_twr": round(cash_pct, 2),
        "top": top,
        "worst": worst,
        "track_record": past,
        "recommendation": action,
    }


def render_notebook(nb: dict) -> str:
    market = nb["market"]
    lines = [
        f"Notebook {nb['notebook_id']} (parent {nb['parent_id'] or '-'}). Explore {nb['explore_start']} to {nb['explore_end']}, "
        f"{nb['sessions']} sessions, {nb['n_specs']} rules tested, {nb['n_distinct']} with distinct trades.",
        "",
        "Market in the explore window:",
        f"- trend {market['trend']} (equal-weight {market['equal_weight_return_pct']}%), "
        f"volatility {market['volatility']} (mean {market['mean_ann_vol_pct']}% annualized)",
    ]
    for symbol, item in market["symbols"].items():
        lines.append(f"- {symbol}: {item['return_pct']:+.2f}%, vol {item['ann_vol_pct']}%, max drawdown {item['max_drawdown_pct']:.2f}%")
    bh = nb["buy_and_hold"]
    lines += [
        f"- buy-and-hold TWR {bh['twr']:.2f}%, drawdown {bh['max_drawdown']:.2f}%. Cash TWR {nb['cash_twr']:.2f}%.",
        "",
        "Top rules (explore window):",
    ]
    for item in nb["top"]:
        lines.append(_rule_line(item))
    lines += ["", "Worst rules and why:"]
    for item in nb["worst"]:
        lines.append(_rule_line(item))
        lines.append("  why: " + "; ".join(item["why"]))
    lines += ["", "Out-of-sample record (test months finished before this test month):"]
    if nb["track_record"]:
        for item in nb["track_record"]:
            lines.append(
                f"- {item['test_month']} {item['name']} (explore rank {item['explore_rank']}): test TWR {item['twr']:.2f}%, "
                f"buy-and-hold {item['bench_twr']:.2f}%, excess {item['excess_pp']:+.2f} pp, drawdown {item['max_drawdown']:.2f}%"
            )
    else:
        lines.append("- none yet. This is the first test month, so no rule has an out-of-sample result.")
    rec = nb["recommendation"]
    conf = rec["confidence"]
    lines += [
        "",
        f"What to do: {rec['instruction']}",
        f"Confidence: {conf['level'].upper()}. {conf['basis']}",
    ]
    return "\n".join(lines)


def advisory_text(nb: dict) -> str:
    """The notebook as advice: market regime, top rules with confidence, and their out-of-sample record.

    Same facts the memory arm reads, without the worst-rule list or an instruction to follow a rule.
    """
    market = nb["market"]
    bh = nb["buy_and_hold"]
    lines = [
        f"Explore window {nb['explore_start']} to {nb['explore_end']}, {nb['sessions']} sessions, "
        f"{nb['n_specs']} rules tested, {nb['n_distinct']} with distinct trades.",
        f"Market regime: trend {market['trend']} (equal-weight {market['equal_weight_return_pct']}%), "
        f"volatility {market['volatility']} (mean {market['mean_ann_vol_pct']}% annualized).",
    ]
    for symbol, item in market["symbols"].items():
        lines.append(f"- {symbol}: {item['return_pct']:+.2f}%, vol {item['ann_vol_pct']}%, max drawdown {item['max_drawdown_pct']:.2f}%")
    lines += [
        f"Buy-and-hold TWR {bh['twr']:.2f}%, drawdown {bh['max_drawdown']:.2f}%. Cash TWR {nb['cash_twr']:.2f}%.",
        "Top rules (explore window):",
    ]
    lines += [_rule_line(item) for item in nb["top"]]
    lines.append("Out-of-sample record (test months finished before this one):")
    if nb["track_record"]:
        for item in nb["track_record"]:
            lines.append(
                f"- {item['test_month']} {item['name']} (explore rank {item['explore_rank']}): test TWR {item['twr']:.2f}%, "
                f"buy-and-hold {item['bench_twr']:.2f}%, excess {item['excess_pp']:+.2f} pp"
            )
    else:
        lines.append("- none yet.")
    rec = nb["recommendation"]
    lines.append(
        f"Notebook's own pick: {rec['rule']} (confidence {rec['confidence']['level']}). {rec['confidence']['basis']}"
    )
    return "\n".join(lines)


def advice_rule_names(nb: dict) -> list[str]:
    """Names the advisory agent can cite: top rules, their same-trade aliases, and rules in the track record."""
    names: list[str] = []
    for item in nb["top"]:
        names += [item["name"], *item.get("same_trades_as", [])]
    names += [item["name"] for item in nb["track_record"]]
    rec = nb["recommendation"].get("rule")
    if rec:
        names.append(rec)
    return list(dict.fromkeys(name for name in names if name))


def _rule_line(item: dict) -> str:
    t = "n/a" if item["daily_excess_t"] is None else f"{item['daily_excess_t']:.2f}"
    oos = item["oos"]
    oos_text = (
        "out-of-sample " + ", ".join(f"{o['test_month']} {o['excess_pp']:+.2f} pp" for o in oos)
        if oos else "no out-of-sample record"
    )
    same = f" Same trades as {', '.join(item['same_trades_as'])}." if item.get("same_trades_as") else ""
    return (
        f"{item['rank']}. {item['name']} [{item['spec_id']}]: {describe_params(item['params'])}. "
        f"TWR {item['twr']:.2f}%, excess {item['excess_pp']:+.2f} pp vs buy-and-hold, drawdown {item['max_drawdown']:.2f}%, "
        f"turnover {item['turnover']:.2f}, avg invested {item['avg_invested_pct']}%, daily excess t {t}, {oos_text}. "
        f"Confidence {item['confidence']['level']}.{same}"
    )


def oos_record(test_month: str, test_start: str, test_end: str, entry: dict, row: dict, bench_row: dict) -> dict:
    return {
        "test_month": test_month,
        "test_start": test_start,
        "test_end": test_end,
        "name": entry["name"],
        "spec_id": entry["spec_id"],
        "signature": rule_signature(entry["spec"]),
        "explore_rank": entry["rank"],
        "twr": round(row.get("twr") or 0.0, 2),
        "bench_twr": round(bench_row.get("twr") or 0.0, 2),
        "excess_pp": round((row.get("twr") or 0.0) - (bench_row.get("twr") or 0.0), 2),
        "max_drawdown": round(row.get("max_drawdown") or 0.0, 2),
        "net_profit": None if row.get("net_profit") is None else round(row["net_profit"], 2),
    }


def rule_view(rules: list[dict], features: Any, targets: Any, runner: Any):
    """Callable for the memory agent: what each notebook rule would order on this account today.

    One runner per rule sees every decision, so its drawdown cut tracks this account.
    """
    runners = {rule["name"]: runner(rule["spec"], features) for rule in rules}

    def view(request: dict, feed: Any) -> list[dict]:
        rows = features(str(request.get("as_of") or ""))
        tradable = list((request.get("data") or {}).get("symbols") or [])
        step = int(request.get("step") or 0)
        out = []
        for index, rule in enumerate(rules):
            spec = rule["spec"]
            every = int(spec.get("rebalance_every") or 1)
            payload = runners[rule["name"]].decide(request, feed)
            out.append({
                **{key: rule[key] for key in ("name", "confidence", "excess_pp")},
                "targets": targets(spec, rows, tradable),
                "orders": [
                    {"symbol": o["symbol"], "side": o["side"], "quantity": o["quantity"], "order_type": "MARKET"}
                    for o in payload.get("orders") or []
                ],
                "why_orders": str(payload.get("reason") or "").removeprefix("rule: "),
                "rebalance_every": every,
                "rebalance_today": step == 0 or step % every == 0,
                "drawdown_cut": spec.get("drawdown_cut"),
                "oos_months": len(rule.get("oos") or []),
                "features": [row for row in rows if row.get("symbol") in tradable] if index == 0 else None,
            })
        return out

    return view


def playbook(nb: dict) -> list[dict]:
    """The rules the memory agent may apply: name, spec, and what the notebook says about it."""
    return [
        {"name": item["name"], "spec": item["spec"], "confidence": item["confidence"]["level"],
         "excess_pp": item["excess_pp"], "oos": item["oos"]}
        for item in nb["top"]
    ]
