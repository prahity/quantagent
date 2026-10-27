"""Explore rules on window E, then test them on a later window T.

The simulator owns cash, fills, and the score. Each spec and each test arm
has its own run. The test notebook is the explore notebook and nothing written
from T. 2025 and 2026 stay closed unless --split validation or --split test.

    python -m meterless.experiment --explore 2024-01..2024-03 --test 2024-04 --rounds 1 --repeats 1
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SIM = REPO / "simulator"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SIM))

from agent.rules import RuleRunner  # noqa: E402
from agent.scripted import AccumulatingBuyAndHold, CashOnlyAgent  # noqa: E402
from meterless.provider import MeterlessRuntime, ProviderError, load_settings  # noqa: E402
from meterless.query import open_analysis_source  # noqa: E402
from meterless.strategy import (  # noqa: E402
    StrategySpec,
    fallback_specs,
    load_features,
    parse_specs,
)
from meterless.trader import NemotronTrader  # noqa: E402
from runner import LocalFeedReader, run_agent  # noqa: E402
from sim.engine import Engine, RunConfig  # noqa: E402
from sim.service import SimulatorService  # noqa: E402
from sim.store import SnapshotStore  # noqa: E402

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_PROPOSE_TOKENS = (2200, 1800)
_NOTE_TOKENS = (2500, 700)
_MEMORY_TOKENS = (3500, 1000)  # one SQL call or one action call: prompt, completion
_NOMEM_TOKENS = (2800, 800)
_CALLS_PER_DECISION = 2  # recall=sql asks for SQL, then for the action
_SECONDS_PER_CALL = 4.7  # April 2024 test: 44 calls per arm in 206s, with two arms overlapping
_ARMS = ("memory", "nomem", "best_rule", "buy_and_hold", "cash")
_CSV_FIELDS = [
    "round", "phase", "arm", "repeat", "window_start", "window_end", "spec_id", "parent_id", "run_id",
    "net_profit", "twr", "max_drawdown", "sharpe", "turnover", "fills", "rejections",
    "tokens", "cost", "wall_time",
]

_PROPOSE = """You write trading rules for a simulator. The goal is to maximize total profit: final equity minus starting cash minus deposits. Cash left uninvested earns the account's cash rate. You cannot create cash.
Each rule is scored later by a deterministic runner over the explore window only. You do not see the test window.
Return one JSON object and no other text:
{{"strategies":[{{"name":"short name","universe":{universe},"signal":{{"kind":"momentum","lookback":21,"max_days_since_filing":null}},"selection":{{"mode":"top_n","top_n":2,"threshold":null}},"target_weight":0.3,"cash_floor":0.1,"rebalance_every":5,"min_weight_change":0.05,"drawdown_cut":0.12}}]}}
signal.kind is momentum, mean_reversion, volatility, relative_strength, or filing_event.
lookback is 5, 21, or 63. The runner ranks released features: 5/21/63-day return, 21-day volatility, 21/63-day MA gap, drawdown from the 63-day high, return minus SPY, days since the last released filing, days until a released filing whose date is still ahead.
target_weight is the fraction for each selected name and must be from 0 to 0.40. cash_floor is the fraction kept in cash.
Write {count} different rules. Do not copy one rule with a renamed field.

Notebook from earlier explore windows only:
{notes}
"""

_FINDINGS = """You are writing the strategy notebook after an explore window. The test window has not been run. Do not mention any date on or after the test, and do not invent numbers.
Quote only numbers that appear in the results. Say what worked, what failed, the sample size, and that the sample is too small to be sure.
Return one JSON object and no other text:
{{"findings":[{{"text":"<one rule>","evidence":"<numbers from the results, the sample size, and the uncertainty>"}}]}}
At most 6 findings.

Results:
{results}
"""


def month_window(value: str) -> tuple[str, str]:
    """`YYYY-MM` or `YYYY-MM-DD` to an inclusive start and end inside that month."""
    if len(value) == 7:
        year, month = map(int, value.split("-"))
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        return start.isoformat(), end.isoformat()
    day = date.fromisoformat(value)
    return day.isoformat(), day.isoformat()


def parse_window(value: str) -> tuple[str, str]:
    """One month, or `YYYY-MM..YYYY-MM` for a span of months."""
    if ".." in value:
        left, right = [part.strip() for part in value.split("..", 1)]
        start, _end_left = month_window(left)
        _start_right, end = month_window(right)
        if start > end:
            raise ValueError(f"window {value} ends before it starts")
        return start, end
    return month_window(value)


def rolling_rounds(year: int = 2024) -> list[dict]:
    """Explore is January through the month before k. Test is month k, April through December.

    March is the first explore end, so every test month has 63 trading sessions of history.
    """
    rounds = []
    for month in range(4, 13):
        explore_end = date(year, month - 1, calendar.monthrange(year, month - 1)[1])
        test_end = date(year, month, calendar.monthrange(year, month)[1])
        rounds.append({
            "round": f"{year}-{month:02d}",
            "explore_start": date(year, 1, 1).isoformat(),
            "explore_end": explore_end.isoformat(),
            "test_start": date(year, month, 1).isoformat(),
            "test_end": test_end.isoformat(),
        })
    return rounds


def assert_allowed_years(start: str, end: str, split: str) -> None:
    """2025 and 2026 stay closed unless a later split is requested on purpose."""
    if split in ("validation", "test"):
        return
    if date.fromisoformat(start).year > 2024 or date.fromisoformat(end).year > 2024:
        raise ValueError("2025 and 2026 stay closed unless --split validation or --split test")


def leaks_test_window(text: str, test_start: str) -> bool:
    return any(match >= test_start for match in _DATE.findall(text or ""))


def notes_for_test(explore_notes: str) -> str:
    """The test agent reads the explore notebook and nothing written from the test window."""
    return explore_notes


def assert_rows_before(rows: list[dict], test_start: str) -> None:
    for row in rows:
        if str(row.get("window_end") or "") >= test_start:
            raise ValueError(f"explore result reaches the test window at {row.get('window_end')}")


class FeatureCache:
    """One feature view per as_of, shared by every isolated account."""

    def __init__(self, source: Any, symbols: list[str]):
        self.source = source
        self.symbols = symbols
        self.cache: dict[str, list[dict]] = {}
        self.lock = threading.Lock()
        self.errors: list[str] = []

    def __call__(self, as_of: str) -> list[dict]:
        key = str(as_of).replace("+00:00", "Z")[:19]
        with self.lock:
            if key not in self.cache:
                try:
                    self.cache[key] = load_features(self.source, as_of, self.symbols)
                    if load_features.last_error and load_features.last_error not in self.errors:
                        self.errors.append(load_features.last_error)
                except Exception as exc:
                    self.errors.append(str(exc).splitlines()[0][-300:])
                    self.cache[key] = []
            return self.cache[key]

    def prefetch(self, as_ofs: list[str]) -> None:
        for index, as_of in enumerate(as_ofs, 1):
            self(as_of)
            if index == 1 or index == len(as_ofs) or index % 5 == 0:
                print(f"features {index}/{len(as_ofs)}", flush=True)


class LockedSource:
    """One JDBC connection. Parallel memory arms take turns."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.lock = threading.Lock()
        self.schema_text = inner.schema_text
        self.data_version = getattr(inner, "data_version", "")
        self.name = getattr(inner, "name", "")

    def execute(self, request: Any, row_limit: int | None = None) -> Any:
        with self.lock:
            return self.inner.execute(request, row_limit=row_limit)


class GatedRuntime:
    """Limit concurrent model calls and back off when the provider asks us to wait."""

    def __init__(self, inner: MeterlessRuntime, gate: threading.Semaphore, retries: int):
        self._inner = inner
        self._gate = gate
        self._retries = retries
        self.last_completion = None

    def complete(self, content: str, model: str | None = None, max_tokens: int | None = None) -> str:
        delay = 2.0
        with self._gate:
            for attempt in range(self._retries + 1):
                try:
                    text = self._inner.complete(content, model=model, max_tokens=max_tokens)
                    self.last_completion = self._inner.last_completion
                    return text
                except ProviderError as exc:
                    self.last_completion = self._inner.last_completion
                    message = str(exc)
                    retryable = any(token in message for token in ("429", "500", "502", "503", "504", "did not answer", "Timeout", "timeout"))
                    if attempt >= self._retries or not retryable:
                        raise
                    time.sleep(delay)
                    delay = min(delay * 2, 60.0)
        raise ProviderError("offline", "runtime did not answer")

    def close(self) -> None:
        self._inner.close()


def _json_blob(text: str) -> Any:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    start_obj, end_obj = raw.find("{"), raw.rfind("}")
    start_list, end_list = raw.find("["), raw.rfind("]")
    if start_obj >= 0 and (start_list < 0 or start_obj < start_list):
        chunk = raw[start_obj : end_obj + 1]
    elif start_list >= 0:
        chunk = raw[start_list : end_list + 1]
    else:
        raise ValueError("model reply had no JSON")
    return json.loads(chunk)


def run_config(start: str, end: str, symbols: list[str], label: str, run_id: str) -> dict:
    return {
        "run_id": run_id,
        "interval": "daily",
        "mode": "V1",
        "start": start,
        "end": end,
        "initial_cash": 10000,
        "daily_deposit": 200,
        "cash_annual_rate": 0.05,
        "symbols": symbols,
        "label": label,
        "execution": {"max_position_pct_equity": 0},
    }


def decision_times(store: SnapshotStore, cfg: dict) -> list[str]:
    engine = Engine(RunConfig.from_defaults(cfg), store)
    engine.reset()
    return [engine.market.boundary(index).isoformat() for index in range(engine.market.steps)]


def simulate(agent: Any, cfg: dict, store: SnapshotStore) -> tuple[dict, float]:
    started = time.perf_counter()
    service = SimulatorService(store, persist=True)
    first = service.reset(cfg)
    feed = LocalFeedReader(service, first["run_id"])
    report = run_agent(agent, lambda _cfg: first, service.submit_action, feed, cfg, verbose=False)
    return report, time.perf_counter() - started


def _tokens(run_id: str) -> tuple[int, int, int]:
    path = SIM / "runs" / run_id / "model_calls.jsonl"
    if not path.exists():
        return 0, 0, 0
    prompt = completion = total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        prompt += int(row.get("prompt_tokens") or 0)
        completion += int(row.get("completion_tokens") or 0)
        if row.get("total_tokens"):
            total += int(row["total_tokens"])
    if total == 0:
        total = prompt + completion
    return prompt, completion, total


def token_rates() -> tuple[float, float]:
    """USD per million input and output tokens. Environment overrides meterless/pricing.yaml."""
    env_in = os.environ.get("NEMOTRON_INPUT_USD_PER_MTOK")
    env_out = os.environ.get("NEMOTRON_OUTPUT_USD_PER_MTOK")
    file_in = file_out = 0.0
    path = REPO / "meterless" / "pricing.yaml"
    if path.exists():
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        file_in = float(loaded.get("input_usd_per_mtok") or 0)
        file_out = float(loaded.get("output_usd_per_mtok") or 0)
    return float(env_in) if env_in not in (None, "") else file_in, float(env_out) if env_out not in (None, "") else file_out


def _cost(prompt: int, completion: int, total: int) -> float:
    in_rate, out_rate = token_rates()
    if in_rate or out_rate:
        return prompt / 1e6 * in_rate + completion / 1e6 * out_rate
    return total / 1e6 * float(os.environ.get("NEMOTRON_USD_PER_MTOK") or 0)


def _row(phase: str, round_id: str, arm: str, repeat: int, start: str, end: str, spec_id: str, parent_id: str | None,
         report: dict | None, wall: float, error: str = "") -> dict:
    prompt = completion = total = 0
    run_id = "" if report is None else report.get("run_id", "")
    if run_id:
        prompt, completion, total = _tokens(run_id)
    return {
        "round": round_id,
        "phase": phase,
        "arm": arm,
        "repeat": repeat,
        "window_start": start,
        "window_end": end,
        "spec_id": spec_id,
        "parent_id": parent_id or "",
        "run_id": run_id,
        "net_profit": None if report is None else report.get("net_profit"),
        "twr": None if report is None else report.get("time_weighted_return_pct"),
        "max_drawdown": None if report is None else report.get("max_drawdown_pct"),
        "sharpe": None if report is None else report.get("sharpe"),
        "turnover": None if report is None else report.get("turnover"),
        "fills": None if report is None else report.get("fills"),
        "rejections": None if report is None else report.get("rejections"),
        "tokens": total,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cost": _cost(prompt, completion, total),
        "wall_time": round(wall, 3),
        "error": error,
    }


def _run_one(agent: Any, cfg: dict, store: SnapshotStore, **meta: Any) -> dict:
    try:
        report, wall = simulate(agent, cfg, store)
        return _row(report=report, wall=wall, **meta)
    except Exception as exc:
        return _row(report=None, wall=0, error=f"{type(exc).__name__}: {exc}", **meta)


def propose_specs(runtime: Any, notes: str, symbols: list[str], count: int, parent_id: str | None) -> list[StrategySpec]:
    prompt = _PROPOSE.format(universe=json.dumps(symbols), count=count, notes=notes or "(empty)")
    specs: list[StrategySpec] = []
    try:
        specs = parse_specs(_json_blob(runtime.complete(prompt, max_tokens=4096)), parent_id, symbols)
    except Exception as exc:
        print(f"propose failed ({type(exc).__name__}); filling the round from fallback rules", flush=True)
    specs = specs[:count]
    have = {spec.name for spec in specs}
    if len(specs) < count:
        for extra in fallback_specs(symbols, count, parent_id):
            if extra.name in have:
                continue
            specs.append(extra)
            have.add(extra.name)
            if len(specs) >= count:
                break
    stem = parent_id or "root"
    for index, spec in enumerate(specs, 1):
        spec.spec_id = f"{stem}-s{index:02d}-{uuid.uuid4().hex[:4]}"
        spec.parent_id = parent_id
    return specs


def _compact(rows: list[dict]) -> list[dict]:
    keep = ("spec_id", "arm", "net_profit", "twr", "max_drawdown", "sharpe", "turnover", "fills", "rejections")
    out = []
    for row in rows:
        item = {key: row.get(key) for key in keep}
        for key, value in item.items():
            if isinstance(value, float):
                item[key] = round(value, 4)
        out.append(item)
    return out


def mechanical_findings(rows: list[dict], sample: dict) -> str:
    """Findings built only from explore numbers, used when the model reply is unusable."""
    ranked = [row for row in rows if row.get("net_profit") is not None and row.get("arm") == "explore"]
    ranked.sort(key=lambda row: row["net_profit"], reverse=True)
    lines = []
    if ranked:
        best, worst = ranked[0], ranked[-1]
        lines.append(
            "r1: The highest explore profit came from "
            f"{best['spec_id']} with net profit {best['net_profit']:.2f}, "
            f"time-weighted return {best['twr']:.2f}, drawdown {best['max_drawdown']:.2f}, "
            f"and excess {best.get('excess', 0):.2f} versus buy-and-hold."
        )
        lines.append(
            f"evidence: sample_days={sample['n_days']} n_specs={sample['n_specs']} "
            f"window {sample['explore_start']} to {sample['explore_end']}. "
            "One window is a small sample, so this ranking is uncertain."
        )
        lines.append(
            f"r2: The weakest explore profit was {worst['spec_id']} at {worst['net_profit']:.2f}. "
            "Do not repeat a rule that lost to cash and to buy-and-hold on this sample."
        )
        lines.append(
            f"evidence: worst net profit {worst['net_profit']:.2f}, drawdown {worst['max_drawdown']:.2f}, "
            f"turnover {worst['turnover']:.4f}. sample_days={sample['n_days']}."
        )
    else:
        lines.append("r1: No explore rule finished, so there is nothing to carry into the test.")
        lines.append(f"evidence: sample_days={sample['n_days']} n_specs={sample['n_specs']}. The sample is empty and uncertain.")
    return "\n".join(lines)


def render_findings(raw: str, evidence: str, sample: dict, rows: list[dict]) -> str:
    try:
        payload = _json_blob(raw)
    except Exception:
        return mechanical_findings(rows, sample)
    findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(findings, list) or not findings:
        return mechanical_findings(rows, sample)
    allowed = set(_NUMBER.findall(evidence))
    blocks = []
    next_id = 1
    for item in findings[:6]:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())
        proof = " ".join(str(item.get("evidence") or "").split())
        if not text or not proof:
            continue
        if not set(_NUMBER.findall(text + " " + proof)) <= allowed:
            continue
        if leaks_test_window(text + " " + proof, sample["test_start"]):
            continue
        blocks.append(f"r{next_id}: {text[:400]}\nevidence: {proof[:400]}")
        next_id += 1
    if not blocks:
        return mechanical_findings(rows, sample)
    if "uncertain" not in raw.lower() and "small sample" not in raw.lower():
        blocks.append(
            f"r{next_id}: Treat the explore ranking as uncertain.\n"
            f"evidence: sample_days={sample['n_days']} n_specs={sample['n_specs']}. One window is a small sample."
        )
    return "\n\n".join(blocks)


def write_findings(runtime: Any, prior: str, rows: list[dict], sample: dict) -> str:
    assert_rows_before(rows, sample["test_start"])
    payload = json.dumps({"sample": {key: sample[key] for key in ("explore_start", "explore_end", "n_days", "n_specs")}, "results": _compact(rows)}, indent=1)
    if leaks_test_window(payload, sample["test_start"]):
        raise ValueError("explore findings payload contains a test-window date")
    try:
        raw = runtime.complete(_FINDINGS.format(results=payload) + "\n\nPrior notebook:\n" + (prior or "(empty)"), max_tokens=1500)
    except Exception as exc:
        print(f"findings call failed ({type(exc).__name__}); writing the numeric summary", flush=True)
        raw = ""
    text = render_findings(raw, payload, sample, rows) if raw else mechanical_findings(rows, sample)
    if leaks_test_window(text, sample["test_start"]):
        text = mechanical_findings(rows, sample)
    return text


def best_explore(rows: list[dict]) -> dict:
    finished = [row for row in rows if row.get("arm") == "explore" and row.get("net_profit") is not None]
    if not finished:
        raise RuntimeError("no explore rule finished")
    return max(finished, key=lambda row: (row["net_profit"], row.get("twr") or -1e9, -(row.get("max_drawdown") or 1e9)))


def _save(out: Path, kind: str, payload: dict) -> None:
    path = out / "history.jsonl"
    payload = {"kind": kind, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str) + "\n")


def _spec_dump(out: Path, spec: StrategySpec) -> None:
    directory = out / "specs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{spec.spec_id}.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    _save(out, "spec", spec.model_dump())


def planned_llm(rounds: list[dict], n_rounds: int, repeats: int, explore_days: list[int], test_days: list[int],
                 llm_concurrency: int = 2) -> dict:
    """Count model calls. Rule backtests, buy-and-hold, and cash call no model.

    Each test decision is two calls: the SQL the model writes, then the action.
    """
    propose = len(rounds) * n_rounds
    notes = len(rounds) * n_rounds
    decisions = sum(test_days) * repeats
    memory = decisions * _CALLS_PER_DECISION
    nomem = memory
    calls = propose + notes + memory + nomem
    prompt = (
        propose * _PROPOSE_TOKENS[0]
        + notes * _NOTE_TOKENS[0]
        + memory * _MEMORY_TOKENS[0]
        + nomem * _NOMEM_TOKENS[0]
    )
    completion = (
        propose * _PROPOSE_TOKENS[1]
        + notes * _NOTE_TOKENS[1]
        + memory * _MEMORY_TOKENS[1]
        + nomem * _NOMEM_TOKENS[1]
    )
    in_rate, out_rate = token_rates()
    workers = max(int(llm_concurrency), 1)
    return {
        "rounds": rounds,
        "explore_rounds_each": n_rounds,
        "propose_calls": propose,
        "notebook_calls": notes,
        "memory_decision_calls": memory,
        "nomem_decision_calls": nomem,
        "calls_per_decision": _CALLS_PER_DECISION,
        "rule_buyhold_cash_calls": 0,
        "llm_calls": calls,
        "estimated_prompt_tokens": prompt,
        "estimated_completion_tokens": completion,
        "estimated_tokens": prompt + completion,
        "input_usd_per_mtok": in_rate,
        "output_usd_per_mtok": out_rate,
        "estimated_cost": prompt / 1e6 * in_rate + completion / 1e6 * out_rate,
        "estimated_wall_seconds": calls / workers * _SECONDS_PER_CALL,
        "seconds_per_call": _SECONDS_PER_CALL,
        "llm_concurrency": workers,
        "explore_days": explore_days,
        "test_days": test_days,
    }


def print_plan(plan: dict) -> None:
    print("dry run, no model calls", flush=True)
    for spec, explore_n, test_n in zip(plan["rounds"], plan["explore_days"], plan["test_days"]):
        print(
            f"  {spec['round']}  explore {spec['explore_start']}..{spec['explore_end']} ({explore_n} sessions)  "
            f"test {spec['test_start']}..{spec['test_end']} ({test_n} sessions)",
            flush=True,
        )
    print(
        f"propose {plan['propose_calls']}  notebook {plan['notebook_calls']}  "
        f"memory calls {plan['memory_decision_calls']}  nomem calls {plan['nomem_decision_calls']}  "
        f"({plan['calls_per_decision']} per decision)  "
        f"rule/buy-and-hold/cash model calls {plan['rule_buyhold_cash_calls']}",
        flush=True,
    )
    print(
        f"estimated tokens {plan['estimated_tokens']}  "
        f"rates ${plan['input_usd_per_mtok']}/M input ${plan['output_usd_per_mtok']}/M output  "
        f"estimated cost ${plan['estimated_cost']:.4f}",
        flush=True,
    )
    print(
        f"estimated model wall {plan['estimated_wall_seconds'] / 60:.1f} min "
        f"at {plan['seconds_per_call']}s per call and concurrency {plan['llm_concurrency']} "
        f"(rule backtests are local and not in this figure)",
        flush=True,
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in _CSV_FIELDS})


def _svg(path: Path, title: str, body: str, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
        f'<text x="16" y="24" font-family="sans-serif" font-size="16">{title}</text>{body}</svg>',
        encoding="utf-8",
    )


def write_charts(directory: Path, rows: list[dict]) -> None:
    test_rows = [row for row in rows if row.get("phase") == "test" and row.get("net_profit") is not None]
    months = sorted({row["round"] for row in test_rows})
    colors = {"memory": "#1f4e79", "nomem": "#6fa8dc", "best_rule": "#b45f06", "buy_and_hold": "#38761d", "cash": "#666666"}
    width, height = 960, 420
    if not months:
        _svg(directory / "net_profit.svg", "Net profit by arm", "", width, height)
        _svg(directory / "cumulative_excess.svg", "Cumulative excess versus buy-and-hold", "", width, height)
        return
    group = 140
    left, bottom, top = 60, 360, 50
    parts = []
    values = [float(row["net_profit"]) for row in test_rows]
    lo, hi = min(0, min(values)), max(0, max(values))
    span = hi - lo or 1
    for month_index, month in enumerate(months):
        month_rows = [row for row in test_rows if row["round"] == month]
        for arm_index, arm in enumerate(_ARMS):
            arm_rows = [row for row in month_rows if row["arm"] == arm]
            if not arm_rows:
                continue
            value = sum(float(row["net_profit"]) for row in arm_rows) / len(arm_rows)
            x = left + month_index * group + arm_index * 22
            y = bottom - (value - lo) / span * (bottom - top)
            zero = bottom - (0 - lo) / span * (bottom - top)
            y0, y1 = sorted((y, zero))
            parts.append(f'<rect x="{x}" y="{y0:.1f}" width="18" height="{max(y1 - y0, 1):.1f}" fill="{colors[arm]}"/>')
        parts.append(f'<text x="{left + month_index * group}" y="390" font-size="12" font-family="sans-serif">{month}</text>')
    legend = "".join(
        f'<rect x="{left + index * 130}" y="40" width="12" height="12" fill="{colors[arm]}"/>'
        f'<text x="{left + 16 + index * 130}" y="50" font-size="12" font-family="sans-serif">{arm}</text>'
        for index, arm in enumerate(_ARMS)
    )
    _svg(directory / "net_profit.svg", "Net profit by arm", legend + "".join(parts), width, height)

    lines = []
    for arm, color in (("memory", colors["memory"]), ("nomem", colors["nomem"])):
        cumulative = 0.0
        points = []
        for index, month in enumerate(months):
            agent = [row for row in test_rows if row["round"] == month and row["arm"] == arm and row.get("twr") is not None]
            bench = [row for row in test_rows if row["round"] == month and row["arm"] == "buy_and_hold" and row.get("twr") is not None]
            if not agent or not bench:
                continue
            excess = sum(float(row["twr"]) for row in agent) / len(agent) - sum(float(row["twr"]) for row in bench) / len(bench)
            cumulative += excess
            points.append((left + index * group, cumulative))
        if not points:
            continue
        hi = max(abs(point[1]) for point in points) or 1
        coords = " ".join(f"{x:.1f},{bottom - (value / hi) * (bottom - top) * 0.5 - (bottom - top) * 0.25:.1f}" for x, value in points)
        lines.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{coords}"/>')
        lines.append(
            f'<text x="{left}" y="{70 + (0 if arm == "memory" else 18)}" font-size="12" font-family="sans-serif" fill="{color}">{arm}</text>'
        )
    _svg(directory / "cumulative_excess.svg", "Cumulative excess versus buy-and-hold", "".join(lines), width, height)


def run_explore(out: Path, store: SnapshotStore, source: Any, runtime: Any, symbols: list[str], spec: dict,
                notes: str, rounds: int, specs_per_round: int, workers: int) -> tuple[str, str, list[dict]]:
    """Return the notebook body, its id, and every explore row including buy-and-hold."""
    start, end = spec["explore_start"], spec["explore_end"]
    round_id = spec["round"]
    notebook_id = ""
    body = notes
    rows: list[dict] = []
    bench_cfg = run_config(start, end, symbols, f"{round_id}-bench", f"bench_{round_id}_{uuid.uuid4().hex[:6]}")
    print(f"explore {start}..{end} buy-and-hold", flush=True)
    bench = _run_one(
        AccumulatingBuyAndHold(), bench_cfg, store,
        phase="explore", round_id=round_id, arm="buy_and_hold", repeat=1,
        start=start, end=end, spec_id="buy_and_hold", parent_id=notebook_id or None,
    )
    features = FeatureCache(source, symbols)
    times = decision_times(store, run_config(start, end, symbols, "calendar", f"cal_{uuid.uuid4().hex[:6]}"))
    features.prefetch(times)
    for round_index in range(1, rounds + 1):
        parent = notebook_id or None
        proposed = propose_specs(runtime, body, symbols, specs_per_round, parent)
        print(f"explore round {round_index}  {len(proposed)} specs  workers {workers}", flush=True)
        jobs = []
        for index, rule in enumerate(proposed, 1):
            _spec_dump(out, rule)
            cfg = run_config(start, end, symbols, rule.name, f"ex_{round_id}_r{round_index}_{index:02d}_{uuid.uuid4().hex[:4]}")
            jobs.append((rule, cfg))

        def one(job: tuple[StrategySpec, dict]) -> dict:
            rule, cfg = job
            row = _run_one(
                RuleRunner(rule.model_dump(), features), cfg, store,
                phase="explore", round_id=round_id, arm="explore", repeat=round_index,
                start=start, end=end, spec_id=rule.spec_id, parent_id=rule.parent_id,
            )
            row["name"] = rule.name
            row["origin"] = rule.origin
            if row.get("twr") is not None and bench.get("twr") is not None:
                row["excess"] = row["twr"] - bench["twr"]
            _save(out, "result", row)
            print(f"  {rule.name} net {row.get('net_profit')}", flush=True)
            return row

        with ThreadPoolExecutor(max_workers=workers) as pool:
            round_rows = list(pool.map(one, jobs))
        rows.extend(round_rows)
        sample = {
            "explore_start": start,
            "explore_end": end,
            "test_start": spec["test_start"],
            "n_days": len(times),
            "n_specs": len(round_rows),
        }
        notebook_id = f"nb-{round_id}-r{round_index}-{uuid.uuid4().hex[:4]}"
        body = write_findings(runtime, body, round_rows + [bench], sample)
        directory = out / "notebooks"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{notebook_id}.md").write_text(f"id: {notebook_id}\nparent: {parent or ''}\n\n{body}", encoding="utf-8")
        _save(out, "notebook", {"notebook_id": notebook_id, "parent_id": parent, "round": round_id, "round_index": round_index})
        print(f"notebook {notebook_id} parent {parent or '-'}", flush=True)
    rows.append(bench)
    return body, notebook_id, rows


def run_test(out: Path, store: SnapshotStore, source: Any, runtime_factory: Any, symbols: list[str], spec: dict,
             notes: str, notebook_id: str, best: dict, repeats: int, workers: int) -> list[dict]:
    start, end = spec["test_start"], spec["test_end"]
    test_notes = notes_for_test(notes)
    if leaks_test_window(test_notes, start):
        raise ValueError("test notebook contains a date from the test window")
    features = FeatureCache(source, symbols)
    features.prefetch(decision_times(store, run_config(start, end, symbols, "calendar", f"cal_{uuid.uuid4().hex[:6]}")))
    spec_payload = json.loads((out / "specs" / f"{best['spec_id']}.json").read_text(encoding="utf-8"))
    jobs: list[tuple[str, int, Any, str]] = []
    for repeat in range(1, repeats + 1):
        memory = NemotronTrader(runtime_factory(), memory=5, notes=test_notes, retries=0, source=source, recall="sql")
        nomem = NemotronTrader(runtime_factory(), memory=0, notes="", retries=0, source=source, recall="sql")
        jobs.append(("memory", repeat, memory, notebook_id))
        jobs.append(("nomem", repeat, nomem, ""))
    jobs.append(("best_rule", 1, RuleRunner(spec_payload, features), best["spec_id"]))
    jobs.append(("buy_and_hold", 1, AccumulatingBuyAndHold(), "buy_and_hold"))
    jobs.append(("cash", 1, CashOnlyAgent(), "cash"))

    def one(job: tuple[str, int, Any, str]) -> dict:
        arm, repeat, agent, parent = job
        cfg = run_config(start, end, symbols, f"{spec['round']}-{arm}-{repeat}", f"te_{spec['round']}_{arm}_{repeat}_{uuid.uuid4().hex[:4]}")
        row = _run_one(
            agent, cfg, store,
            phase="test", round_id=spec["round"], arm=arm, repeat=repeat,
            start=start, end=end, spec_id=parent or arm, parent_id=notebook_id if arm == "memory" else parent,
        )
        _save(out, "result", row)
        print(
            f"test {arm}#{repeat} net {row.get('net_profit')} twr {row.get('twr')} "
            f"dd {row.get('max_drawdown')} tokens {row.get('tokens')} wall {row.get('wall_time')}",
            flush=True,
        )
        close = getattr(getattr(agent, "runtime", None), "close", None)
        if close:
            close()
        return row

    with ThreadPoolExecutor(max_workers=max(workers, len(jobs))) as pool:
        return list(pool.map(one, jobs))


def print_table(rows: list[dict]) -> None:
    test_rows = [row for row in rows if row.get("phase") == "test"]
    print(f"{'arm':16} {'rep':3} {'net_profit':>12} {'twr':>8} {'max_dd':>8} {'sharpe':>8} {'turnover':>8} {'fills':>5} {'rej':>4} {'tokens':>8} {'cost':>8} {'wall_s':>8}", flush=True)
    for row in test_rows:
        sharpe = row.get("sharpe")
        print(
            f"{row['arm']:16} {row['repeat']:3} "
            f"{_fmt(row.get('net_profit')):>12} {_fmt(row.get('twr')):>8} {_fmt(row.get('max_drawdown')):>8} "
            f"{'' if sharpe is None else f'{sharpe:.2f}':>8} {_fmt(row.get('turnover')):>8} "
            f"{row.get('fills') or 0:5} {row.get('rejections') or 0:4} {row.get('tokens') or 0:8} "
            f"{float(row.get('cost') or 0):8.4f} {float(row.get('wall_time') or 0):8.1f}",
            flush=True,
        )


def _fmt(value: Any) -> str:
    return "" if value is None else f"{float(value):.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(REPO / "context" / "experiments" / "explore-2024"))
    parser.add_argument("--explore", default="2024-01", help="explore month YYYY-MM, or YYYY-MM..YYYY-MM")
    parser.add_argument("--test", default="2024-02", help="test month YYYY-MM, strictly after explore")
    parser.add_argument("--rolling", action="store_true", help="explore Jan..month k-1, test month k, for February..December 2024")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--specs", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--llm-concurrency", type=int, default=2)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--symbols", default="AAPL,MSFT,SPY")
    parser.add_argument("--split", default="", choices=["", "validation", "test"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default="")
    parser.add_argument("--thinking", default="off", choices=["on", "off"])
    args = parser.parse_args()
    symbols = [part.strip().upper() for part in args.symbols.split(",") if part.strip()]
    if args.rolling:
        rounds = rolling_rounds(2024)
    else:
        explore_start, explore_end = parse_window(args.explore)
        test_start, test_end = parse_window(args.test)
        if test_start <= explore_end:
            raise SystemExit(f"test window {test_start} must start after explore end {explore_end}")
        rounds = [{
            "round": args.test if len(args.test) == 7 else test_start[:7],
            "explore_start": explore_start,
            "explore_end": explore_end,
            "test_start": test_start,
            "test_end": test_end,
        }]
    for spec in rounds:
        assert_allowed_years(spec["explore_start"], spec["test_end"], args.split)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    store = SnapshotStore.load(SIM / "sim" / "snapshot")
    explore_days = [len(decision_times(store, run_config(spec["explore_start"], spec["explore_end"], symbols, "dry", f"dry_e_{index}"))) for index, spec in enumerate(rounds)]
    test_days = [len(decision_times(store, run_config(spec["test_start"], spec["test_end"], symbols, "dry", f"dry_t_{index}"))) for index, spec in enumerate(rounds)]
    plan = planned_llm(rounds, args.rounds, args.repeats, explore_days, test_days, args.llm_concurrency)
    if args.dry_run:
        print_plan(plan)
        return
    settings = load_settings()
    model = args.model or settings["model"]
    extra = {} if args.thinking == "on" else {"chat_template_kwargs": {"enable_thinking": False}}
    gate = threading.Semaphore(args.llm_concurrency)
    opened = open_analysis_source(snapshot_dir=SIM / "sim" / "snapshot")
    opened.feature_symbols = list(symbols)
    source = LockedSource(opened)

    def runtime_factory() -> GatedRuntime:
        inner = MeterlessRuntime(settings["base_url"], settings["meterless_runtime_token"], model, extra_body=extra)
        return GatedRuntime(inner, gate, args.retries)

    planner = runtime_factory()
    notes = ""
    all_rows: list[dict] = []
    try:
        for spec in rounds:
            print(f"round {spec['round']} explore {spec['explore_start']}..{spec['explore_end']} test {spec['test_start']}..{spec['test_end']}", flush=True)
            notes, notebook_id, explore_rows = run_explore(
                out, store, source, planner, symbols, spec, notes, args.rounds, args.specs, args.workers,
            )
            all_rows.extend(explore_rows)
            winner = best_explore(explore_rows)
            print(f"best explore spec {winner['spec_id']} net {winner.get('net_profit')}", flush=True)
            all_rows.extend(run_test(
                out, store, source, runtime_factory, symbols, spec, notes, notebook_id, winner, args.repeats, args.workers,
            ))
    finally:
        planner.close()
    write_csv(out / "results.csv", all_rows)
    write_charts(out / "charts", all_rows)
    print_table(all_rows)
    print(f"wrote {out / 'results.csv'}", flush=True)


if __name__ == "__main__":
    main()
