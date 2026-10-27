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
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SIM = REPO / "simulator"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SIM))

from agent.rules import RuleRunner, _targets  # noqa: E402
from agent.scripted import AccumulatingBuyAndHold, CashOnlyAgent  # noqa: E402
from meterless import explore_notebook  # noqa: E402
from meterless.provider import MeterlessRuntime, ProviderError, load_settings  # noqa: E402
from meterless.query import LakeError, open_analysis_source  # noqa: E402
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
_PROPOSE_ATTEMPTS = 3
# Measured on the April 2024 round through Meterless (jan-mar-apr-2024-v4), thinking off.
_PROPOSE_TOKENS = (2200, 1300)  # prompt, completion per propose call
_PROPOSE_CALLS_PER_ROUND = 1.5  # one reply in three failed to parse before repair
_PROPOSE_SECONDS_PER_CALL = 19.0
_MEMORY_TOKENS = (7372, 157)  # per call: the notebook and rule orders make the prompt long
_NOMEM_TOKENS = (2846, 143)
_MEMORY_CALLS_PER_DECISION = 1.15  # 25 calls for 22 decisions: the rule orders make SQL optional
_NOMEM_CALLS_PER_DECISION = 2.0  # SQL, then the action
_MEMORY_SECONDS_PER_CALL = 2.9  # arm wall / calls: model latency plus SQL and simulator steps
_NOMEM_SECONDS_PER_CALL = 2.2
_SECONDS_PER_FEATURE_DAY = 2.5  # one JDBC feature view per session (measured 2.1 to 3.7)
_BACKTEST_SECONDS_PER_SESSION = 0.08  # one rule over one session, per worker
_CASH_RATE = 0.05
_ARMS = ("memory", "nomem", "best_rule", "buy_and_hold", "cash")
_CSV_FIELDS = [
    "round", "phase", "arm", "repeat", "window_start", "window_end", "spec_id", "parent_id", "run_id",
    "net_profit", "twr", "max_drawdown", "sharpe", "turnover", "fills", "rejections",
    "tokens", "cost", "wall_time", "name", "origin",
]
# Set by the first lake failure so the other arms stop at their next lake call instead of running on.
_LAKE_DOWN = threading.Event()


def _raise_if_lake_down() -> None:
    if _LAKE_DOWN.is_set():
        raise LakeError("stopped: an earlier lake call in this round failed")


_PROPOSAL_FIELDS = ["round", "explore_round", "nemotron", "fallback", "propose_calls"]
_RESUME_KEYS = ("symbols", "rounds", "explore_rounds", "specs", "repeats", "model", "thinking", "data_version")

_PROPOSE = """You write trading rules for a simulator. The goal is to maximize total profit: final equity minus starting cash minus deposits. Cash left uninvested earns the account's cash rate. You cannot create cash.
Each rule is scored later by a deterministic runner over the explore window only. You do not see the test window.
Return one JSON object and no other text:
{{"strategies":[{{"name":"short name","universe":{universe},"signal":{{"kind":"momentum","lookback":21,"max_days_since_filing":null}},"selection":{{"mode":"top_n","top_n":2,"threshold":null}},"target_weight":0.3,"cash_floor":0.1,"rebalance_every":5,"min_weight_change":0.05,"drawdown_cut":0.12}}]}}
signal.kind is momentum, mean_reversion, volatility, relative_strength, or filing_event.
lookback is 5, 21, or 63. The runner ranks released features: 5/21/63-day return, 21-day volatility, 21/63-day MA gap, drawdown from the 63-day high, return minus SPY, days since the last released filing, days until a released filing whose date is still ahead.
target_weight is the fraction for each selected name and must be from 0 to 0.40. cash_floor is the fraction kept in cash.
Write {count} different rules. Do not copy one rule with a renamed field. Each rule is its own {{...}} object inside the strategies list, with only the keys shown.

Notebook from earlier explore windows only:
{notes}
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


def feature_cache_path(backend: str, data_version: str, symbols: list[str]) -> Path:
    """One file per backend, snapshot version, and symbol list. A new snapshot starts a new file."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", data_version or "unpinned")
    return REPO / "context" / "feature_cache" / (backend or "source") / safe / f"{'-'.join(symbols)}.jsonl"


def _cache_key(as_of: Any) -> str:
    return str(as_of).replace("+00:00", "Z")[:19]


class FeatureCache:
    """One feature view per as_of, shared by every isolated account and every round.

    With ``path`` set, each view is appended to that JSON lines file and read back on
    the next run, so a session is fetched from the lake once per snapshot version.
    A view that failed raises LakeError and is neither cached nor written.
    """

    def __init__(self, source: Any, symbols: list[str], path: Path | None = None):
        self.source = source
        self.symbols = symbols
        self.path = path
        self.cache: dict[str, list[dict]] = {}
        self.lock = threading.Lock()
        self.errors: list[str] = []
        self.hits = 0
        self.fetched = 0
        self.loaded = 0
        if path is not None and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self.cache[entry["as_of"]] = entry["rows"]
            self.loaded = len(self.cache)

    def __contains__(self, as_of: Any) -> bool:
        return _cache_key(as_of) in self.cache

    def __call__(self, as_of: str) -> list[dict]:
        key = _cache_key(as_of)
        with self.lock:
            if key in self.cache:
                self.hits += 1
                return self.cache[key]
            _raise_if_lake_down()
            try:
                rows = load_features(self.source, as_of, self.symbols)
            except LakeError:
                _LAKE_DOWN.set()
                raise
            except Exception as exc:
                _LAKE_DOWN.set()
                raise LakeError(f"feature view at {as_of} failed: {type(exc).__name__}: {exc}") from exc
            if load_features.last_error and load_features.last_error not in self.errors:
                self.errors.append(load_features.last_error)
            self.cache[key] = rows
            self.fetched += 1
            self._persist(key, rows)
            return rows

    def _persist(self, key: str, rows: list[dict]) -> None:
        if self.path is None:
            return
        try:
            line = json.dumps({"as_of": key, "rows": rows}, allow_nan=True)
        except (TypeError, ValueError):
            return
        if json.loads(line)["rows"] != rows:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def prefetch(self, as_ofs: list[str]) -> None:
        started = time.perf_counter()
        missing = [as_of for as_of in as_ofs if as_of not in self]
        for index, as_of in enumerate(missing, 1):
            self(as_of)
            if index == 1 or index == len(missing) or index % 5 == 0:
                print(f"features {index}/{len(missing)} fetched {time.perf_counter() - started:.1f}s", flush=True)
        print(f"features {len(as_ofs) - len(missing)}/{len(as_ofs)} from cache, {len(missing)} fetched", flush=True)


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
            _raise_if_lake_down()
            try:
                return self.inner.execute(request, row_limit=row_limit)
            except LakeError:
                _LAKE_DOWN.set()
                raise


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
        "cash_annual_rate": _CASH_RATE,
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
    _raise_if_lake_down()
    try:
        report, wall = simulate(agent, cfg, store)
        return _row(report=report, wall=wall, **meta)
    except LakeError:
        _LAKE_DOWN.set()
        raise
    except Exception as exc:
        return _row(report=None, wall=0, error=f"{type(exc).__name__}: {exc}", **meta)


def _repair_strategies(text: str) -> str:
    """Nemotron sometimes drops the `{` before the next rule, or leaves a key after a closed rule."""
    text = re.sub(r'\}\s*,\s*"description"\s*:\s*"(?:[^"\\]|\\.)*"\s*\}', "}", text)
    return re.sub(r'\}\s*,\s*"name"\s*:', '},{"name":', text)


def propose_specs(runtime: Any, notes: str, symbols: list[str], count: int, parent_id: str | None,
                  attempts: int = _PROPOSE_ATTEMPTS) -> list[StrategySpec]:
    prompt = _PROPOSE.format(universe=json.dumps(symbols), count=count, notes=notes or "(empty)")
    specs: list[StrategySpec] = []
    propose_specs.last_calls = 0
    for attempt in range(1, attempts + 1):
        propose_specs.last_calls = attempt
        try:
            text = runtime.complete(prompt, max_tokens=4096)
        except Exception as exc:
            print(f"propose call {attempt} failed ({type(exc).__name__})", flush=True)
            continue
        for candidate in (text, _repair_strategies(text)):
            try:
                specs = parse_specs(_json_blob(candidate), parent_id, symbols)
            except Exception:
                continue
            if specs:
                break
        if specs:
            break
        print(f"propose reply {attempt} was not valid JSON", flush=True)
    if not specs:
        print("propose failed; filling the round from fallback rules", flush=True)
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


propose_specs.last_calls = 0


def write_notebook(out: Path, store: SnapshotStore, symbols: list[str], spec: dict, rows: list[dict], bench: dict,
                   specs: dict[str, dict], sessions: int, track: list[dict], notebook_id: str, parent: str | None) -> dict:
    """Build the explore notebook from explore runs and finished test months, then save it as JSON and text."""
    assert_rows_before(rows + [bench], spec["test_start"])
    nb = explore_notebook.build_notebook(
        notebook_id=notebook_id,
        parent_id=parent,
        round_id=spec["round"],
        explore_start=spec["explore_start"],
        explore_end=spec["explore_end"],
        test_start=spec["test_start"],
        sessions=sessions,
        rows=rows,
        bench=bench,
        specs=specs,
        market=explore_notebook.market_conditions(store, symbols, spec["explore_start"], spec["explore_end"]),
        track=track,
        runs_dir=SIM / "runs",
        cash_annual_rate=_CASH_RATE,
    )
    nb["text"] = explore_notebook.render_notebook(nb)
    if leaks_test_window(json.dumps(nb, default=str), spec["test_start"]):
        raise ValueError("explore notebook contains a date from the test window")
    directory = out / "notebooks"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{notebook_id}.md").write_text(nb["text"] + "\n", encoding="utf-8")
    (directory / f"{notebook_id}.json").write_text(json.dumps(nb, indent=2, default=str), encoding="utf-8")
    return nb


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
                 llm_concurrency: int = 2, specs_per_round: int = 12, workers: int = 4,
                 unique_days: int | None = None) -> dict:
    """Count model calls and wall time. Rule backtests, buy-and-hold, and cash call no model.

    A no-memory decision is two calls (SQL, then the action). The memory agent usually
    answers in one because the rule orders are in its prompt. The notebook calls no model.
    """
    propose = round(len(rounds) * n_rounds * _PROPOSE_CALLS_PER_ROUND)
    decisions = sum(test_days) * repeats
    memory = round(decisions * _MEMORY_CALLS_PER_DECISION)
    nomem = round(decisions * _NOMEM_CALLS_PER_DECISION)
    calls = propose + memory + nomem
    prompt = propose * _PROPOSE_TOKENS[0] + memory * _MEMORY_TOKENS[0] + nomem * _NOMEM_TOKENS[0]
    completion = propose * _PROPOSE_TOKENS[1] + memory * _MEMORY_TOKENS[1] + nomem * _NOMEM_TOKENS[1]
    in_rate, out_rate = token_rates()
    gate = max(int(llm_concurrency), 1)
    feature_days = sum(explore_days) + sum(test_days)
    propose_seconds = propose * _PROPOSE_SECONDS_PER_CALL
    test_seconds = (memory * _MEMORY_SECONDS_PER_CALL + nomem * _NOMEM_SECONDS_PER_CALL) / gate
    backtest_seconds = sum(explore_days) * n_rounds * specs_per_round * _BACKTEST_SECONDS_PER_SESSION / max(workers, 1)
    feature_seconds = feature_days * _SECONDS_PER_FEATURE_DAY
    cached_seconds = (unique_days if unique_days is not None else feature_days) * _SECONDS_PER_FEATURE_DAY
    base = propose_seconds + test_seconds + backtest_seconds
    return {
        "rounds": rounds,
        "explore_rounds_each": n_rounds,
        "repeats": repeats,
        "propose_calls": propose,
        "notebook_calls": 0,
        "memory_decision_calls": memory,
        "nomem_decision_calls": nomem,
        "rule_buyhold_cash_calls": 0,
        "llm_calls": calls,
        "estimated_prompt_tokens": prompt,
        "estimated_completion_tokens": completion,
        "estimated_tokens": prompt + completion,
        "input_usd_per_mtok": in_rate,
        "output_usd_per_mtok": out_rate,
        "estimated_cost": prompt / 1e6 * in_rate + completion / 1e6 * out_rate,
        "estimated_propose_seconds": propose_seconds,
        "estimated_test_model_seconds": test_seconds,
        "estimated_wall_seconds": propose_seconds + test_seconds,
        "estimated_backtest_seconds": backtest_seconds,
        "feature_days": feature_days,
        "unique_feature_days": unique_days if unique_days is not None else feature_days,
        "seconds_per_feature_day": _SECONDS_PER_FEATURE_DAY,
        "estimated_feature_seconds": feature_seconds,
        "estimated_cached_feature_seconds": cached_seconds,
        "estimated_total_seconds": base + feature_seconds,
        "estimated_total_cached_seconds": base + cached_seconds,
        "llm_concurrency": gate,
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
        f"model calls {plan['llm_calls']}: propose {plan['propose_calls']}  notebook {plan['notebook_calls']}  "
        f"memory {plan['memory_decision_calls']}  nomem {plan['nomem_decision_calls']}  "
        f"rule/buy-and-hold/cash {plan['rule_buyhold_cash_calls']}  (repeats {plan['repeats']}, "
        f"{plan['explore_rounds_each']} explore rounds per month, LLM concurrency {plan['llm_concurrency']})",
        flush=True,
    )
    print(
        f"estimated tokens {plan['estimated_tokens']}  "
        f"rates ${plan['input_usd_per_mtok']}/M input ${plan['output_usd_per_mtok']}/M output  "
        f"estimated cost ${plan['estimated_cost']:.4f}",
        flush=True,
    )
    print(
        f"wall estimate: feature views {plan['estimated_feature_seconds'] / 60:.1f} min "
        f"({plan['feature_days']} views at {plan['seconds_per_feature_day']}s)  "
        f"propose {plan['estimated_propose_seconds'] / 60:.1f} min  "
        f"explore backtests {plan['estimated_backtest_seconds'] / 60:.1f} min  "
        f"test-arm model calls {plan['estimated_test_model_seconds'] / 60:.1f} min",
        flush=True,
    )
    print(
        f"estimated total with the shared feature cache {plan['estimated_total_cached_seconds'] / 60:.1f} min "
        f"({plan['unique_feature_days']} sessions to fetch, {plan.get('feature_days_on_disk', 0)} already on disk); "
        f"without the cache it would be {plan['estimated_total_seconds'] / 60:.1f} min",
        flush=True,
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in _CSV_FIELDS})


def write_proposals(path: Path, proposals: list[dict]) -> None:
    """Per round and explore round: rules Nemotron proposed versus rules taken from the fallback list."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_PROPOSAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in proposals:
            writer.writerow(row)


def save_round(out: Path, spec: dict, nb: dict, rows: list[dict], records: list[dict],
               proposals: list[dict], seconds: float) -> Path:
    """Everything a resumed run needs from a finished round. Written last, so its presence means done."""
    directory = out / "rounds"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "round": spec["round"],
        "spec": spec,
        "notebook_id": nb["notebook_id"],
        "notebook_text": nb["text"],
        "rows": rows,
        "records": records,
        "proposals": proposals,
        "seconds": round(seconds, 1),
    }
    path = directory / f"{spec['round']}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, default=str, indent=1), encoding="utf-8")
    os.replace(temp, path)
    return path


def load_finished_rounds(out: Path, rounds: list[dict]) -> list[dict]:
    """Finished rounds in run order. Stops at the first gap so later rounds never see a hole in the track."""
    finished = []
    for spec in rounds:
        path = out / "rounds" / f"{spec['round']}.json"
        if not path.exists():
            break
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("spec") != spec:
            raise SystemExit(f"{path} was written for different windows; use a new --out")
        finished.append(payload)
    return finished


def check_run_config(out: Path, config: dict, resume: bool) -> None:
    """A resumed run must use the same settings; a fresh run must not mix into old rounds."""
    path = out / "run_config.json"
    if resume and path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        changed = [key for key in _RESUME_KEYS if saved.get(key) != config.get(key)]
        if changed:
            raise SystemExit(f"--resume settings differ from {path}: {', '.join(changed)}")
    elif not resume and any((out / "rounds").glob("*.json")):
        raise SystemExit(f"{out} already has finished rounds; pass --resume or use a new --out")
    path.write_text(json.dumps(config, indent=1), encoding="utf-8")


class RoundFailed(RuntimeError):
    """A test or out-of-sample arm did not finish, so the month cannot be scored."""


def check_round(round_id: str, test_rows: list[dict]) -> None:
    failed = [row for row in test_rows if row.get("error") or row.get("net_profit") is None]
    if failed:
        first = failed[0]
        raise RoundFailed(
            f"{len(failed)} test arm(s) of {round_id} failed; first {first.get('arm')}#{first.get('repeat')}: "
            f"{first.get('error') or 'no result'}"
        )


def write_outputs(out: Path, rows: list[dict], proposals: list[dict]) -> None:
    write_csv(out / "results.csv", rows)
    write_proposals(out / "proposals.csv", proposals)
    write_charts(out / "charts", rows)


def print_proposals(proposals: list[dict]) -> None:
    by_round: dict[str, list[dict]] = {}
    for row in proposals:
        by_round.setdefault(row["round"], []).append(row)
    for round_id, items in by_round.items():
        nemotron = sum(item["nemotron"] for item in items)
        fallback = sum(item["fallback"] for item in items)
        detail = ", ".join(f"r{item['explore_round']} {item['nemotron']}/{item['fallback']}" for item in items)
        print(f"rules {round_id}: Nemotron {nemotron}, fallback {fallback} ({detail})", flush=True)


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


class Progress:
    """Append one line per phase to progress.jsonl and echo it, with time since the run started."""

    def __init__(self, path: Path):
        self.path = path
        self.started = time.perf_counter()

    def log(self, round_id: str, phase: str, status: str, **extra: Any) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_s": round(time.perf_counter() - self.started, 1),
            "round": round_id,
            "phase": phase,
            "status": status,
            **extra,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
        detail = " ".join(f"{key}={value}" for key, value in extra.items())
        print(f"[progress] {entry['elapsed_s']:8.1f}s  {round_id}  {phase}  {status}  {detail}".rstrip(), flush=True)

    @contextmanager
    def phase(self, round_id: str, phase: str, **extra: Any):
        began = time.perf_counter()
        self.log(round_id, phase, "start", **extra)
        info: dict[str, Any] = {}
        yield info
        self.log(round_id, phase, "done", seconds=round(time.perf_counter() - began, 1), **info)


def run_explore(out: Path, store: SnapshotStore, source: Any, runtime: Any, symbols: list[str], spec: dict,
                notes: str, rounds: int, specs_per_round: int, workers: int,
                track: list[dict] | None = None, features: FeatureCache | None = None,
                progress: Progress | None = None) -> tuple[dict, list[dict], list[dict]]:
    """Return the structured notebook, every explore row including buy-and-hold, and proposal counts."""
    start, end = spec["explore_start"], spec["explore_end"]
    round_id = spec["round"]
    progress = progress or Progress(out / "progress.jsonl")
    notebook_id = ""
    body = notes
    nb: dict = {}
    rows: list[dict] = []
    proposals: list[dict] = []
    specs: dict[str, dict] = {}
    bench_cfg = run_config(start, end, symbols, f"{round_id}-bench", f"bench_{round_id}_{uuid.uuid4().hex[:6]}")
    print(f"explore {start}..{end} buy-and-hold", flush=True)
    bench = _run_one(
        AccumulatingBuyAndHold(), bench_cfg, store,
        phase="explore", round_id=round_id, arm="buy_and_hold", repeat=1,
        start=start, end=end, spec_id="buy_and_hold", parent_id=notebook_id or None,
    )
    features = features if features is not None else FeatureCache(source, symbols)
    times = decision_times(store, run_config(start, end, symbols, "calendar", f"cal_{uuid.uuid4().hex[:6]}"))
    with progress.phase(round_id, "explore_features", sessions=len(times)) as info:
        fetched_before = features.fetched
        features.prefetch(times)
        info["fetched"] = features.fetched - fetched_before
    for round_index in range(1, rounds + 1):
        parent = notebook_id or None
        with progress.phase(round_id, "propose", explore_round=round_index) as info:
            proposed = propose_specs(runtime, body, symbols, specs_per_round, parent)
            counts = {
                "round": round_id,
                "explore_round": round_index,
                "nemotron": sum(1 for rule in proposed if rule.origin == "model"),
                "fallback": sum(1 for rule in proposed if rule.origin == "fallback"),
                "propose_calls": propose_specs.last_calls,
            }
            proposals.append(counts)
            _save(out, "proposals", counts)
            info.update({key: counts[key] for key in ("nemotron", "fallback", "propose_calls")})
        print(f"explore round {round_index}  {len(proposed)} specs  workers {workers}", flush=True)
        jobs = []
        for index, rule in enumerate(proposed, 1):
            _spec_dump(out, rule)
            specs[rule.spec_id] = rule.model_dump()
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

        with progress.phase(round_id, "explore_backtests", explore_round=round_index, specs=len(jobs)):
            with ThreadPoolExecutor(max_workers=workers) as pool:
                round_rows = list(pool.map(one, jobs))
        rows.extend(round_rows)
        notebook_id = f"nb-{round_id}-r{round_index}-{uuid.uuid4().hex[:4]}"
        with progress.phase(round_id, "notebook", explore_round=round_index):
            nb = write_notebook(out, store, symbols, spec, rows, bench, specs, len(times), track or [], notebook_id, parent)
        body = nb["text"]
        _save(out, "notebook", {"notebook_id": notebook_id, "parent_id": parent, "round": round_id, "round_index": round_index})
        print(f"notebook {notebook_id} parent {parent or '-'}", flush=True)
    rows.append(bench)
    return nb, rows, proposals


def run_test(out: Path, store: SnapshotStore, source: Any, runtime_factory: Any, symbols: list[str], spec: dict,
             nb: dict, best: dict, repeats: int, workers: int, features: FeatureCache | None = None,
             progress: Progress | None = None) -> tuple[list[dict], list[dict]]:
    """Return the test rows and the out-of-sample records of this round's top explore rules."""
    start, end = spec["test_start"], spec["test_end"]
    progress = progress or Progress(out / "progress.jsonl")
    notebook_id = nb["notebook_id"]
    test_notes = notes_for_test(nb["text"])
    if leaks_test_window(test_notes, start):
        raise ValueError("test notebook contains a date from the test window")
    features = features if features is not None else FeatureCache(source, symbols)
    times = decision_times(store, run_config(start, end, symbols, "calendar", f"cal_{uuid.uuid4().hex[:6]}"))
    with progress.phase(spec["round"], "test_features", sessions=len(times)) as info:
        fetched_before = features.fetched
        features.prefetch(times)
        info["fetched"] = features.fetched - fetched_before
    spec_payload = json.loads((out / "specs" / f"{best['spec_id']}.json").read_text(encoding="utf-8"))
    jobs: list[tuple[str, int, Any, str]] = []
    for repeat in range(1, repeats + 1):
        view = explore_notebook.rule_view(explore_notebook.playbook(nb), features, _targets, RuleRunner)
        memory = NemotronTrader(runtime_factory(), memory=5, notes=test_notes, retries=0, source=source, recall="sql", rule_view=view)
        nomem = NemotronTrader(runtime_factory(), memory=0, notes="", retries=0, source=source, recall="sql")
        jobs.append(("memory", repeat, memory, notebook_id))
        jobs.append(("nomem", repeat, nomem, ""))
    jobs.append(("best_rule", 1, RuleRunner(spec_payload, features), best["spec_id"]))
    jobs.append(("buy_and_hold", 1, AccumulatingBuyAndHold(), "buy_and_hold"))
    jobs.append(("cash", 1, CashOnlyAgent(), "cash"))
    for entry in nb["top"]:
        if entry["spec_id"] != best["spec_id"]:
            jobs.append((f"oos_rank{entry['rank']}", 1, RuleRunner(entry["spec"], features), entry["spec_id"]))

    def one(job: tuple[str, int, Any, str]) -> dict:
        arm, repeat, agent, parent = job
        cfg = run_config(start, end, symbols, f"{spec['round']}-{arm}-{repeat}", f"te_{spec['round']}_{arm}_{repeat}_{uuid.uuid4().hex[:4]}")
        row = _run_one(
            agent, cfg, store,
            phase="oos" if arm.startswith("oos_") else "test", round_id=spec["round"], arm=arm, repeat=repeat,
            start=start, end=end, spec_id=parent or arm, parent_id=notebook_id if arm == "memory" else parent,
        )
        if isinstance(agent, NemotronTrader):
            row.update(sql_stats(row.get("run_id") or ""))
            row.update(rule_stats(row.get("run_id") or ""))
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

    with progress.phase(spec["round"], "test_arms", jobs=len(jobs)):
        with ThreadPoolExecutor(max_workers=max(workers, len(jobs))) as pool:
            rows = list(pool.map(one, jobs))
    bench_row = next(row for row in rows if row["arm"] == "buy_and_hold")
    records = []
    for entry in nb["top"]:
        arm = "best_rule" if entry["spec_id"] == best["spec_id"] else f"oos_rank{entry['rank']}"
        row = next((item for item in rows if item["arm"] == arm and item.get("twr") is not None), None)
        if row is not None and bench_row.get("twr") is not None:
            records.append(explore_notebook.oos_record(spec["round"], start, end, entry, row, bench_row))
    for record in records:
        _save(out, "oos", record)
    return rows, records


def sql_stats(run_id: str) -> dict:
    """Queries the agent sent and how many ran. A decision counts as answered when any of its queries ran."""
    rows = explore_notebook.read_jsonl(SIM / "runs" / run_id / "queries.jsonl")
    by_decision: dict[str, list[bool]] = {}
    for row in rows:
        by_decision.setdefault(str(row.get("request_id")), []).append(not row.get("error"))
    return {
        "sql_queries": len(rows),
        "sql_ok": sum(1 for row in rows if not row.get("error")),
        "sql_decisions": len(by_decision),
        "sql_first_try_ok": sum(1 for oks in by_decision.values() if oks and oks[0]),
        "sql_decisions_answered": sum(1 for oks in by_decision.values() if any(oks)),
        "sql_repairs": sum(max(len(oks) - 1, 0) for oks in by_decision.values()),
    }


def rule_stats(run_id: str) -> dict:
    rows = [row for row in explore_notebook.read_jsonl(SIM / "runs" / run_id / "rule_choices.jsonl") if "rule" in row]
    if not rows:
        return {}
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["rule"]] = counts.get(row["rule"], 0) + 1
    return {"rule_counts": counts}


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
    parser.add_argument("--resume", action="store_true", help="skip rounds already saved under OUT/rounds")
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
    explore_times = [decision_times(store, run_config(spec["explore_start"], spec["explore_end"], symbols, "dry", f"dry_e_{index}")) for index, spec in enumerate(rounds)]
    test_times = [decision_times(store, run_config(spec["test_start"], spec["test_end"], symbols, "dry", f"dry_t_{index}")) for index, spec in enumerate(rounds)]
    explore_days = [len(times) for times in explore_times]
    test_days = [len(times) for times in test_times]
    distinct = {_cache_key(moment) for times in explore_times + test_times for moment in times}
    backend = os.environ.get("ANALYSIS_BACKEND", "zetaris").strip().lower()
    on_disk = len(distinct & set(FeatureCache(None, symbols, feature_cache_path(backend, store.data_version, symbols)).cache))
    plan = planned_llm(rounds, args.rounds, args.repeats, explore_days, test_days, args.llm_concurrency,
                       args.specs, args.workers, len(distinct) - on_disk)
    plan["feature_days_on_disk"] = on_disk
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

    check_run_config(out, {
        "symbols": symbols,
        "rounds": [spec["round"] for spec in rounds],
        "explore_rounds": args.rounds,
        "specs": args.specs,
        "repeats": args.repeats,
        "model": model,
        "thinking": args.thinking,
        "data_version": store.data_version,
        "backend": opened.name,
        "llm_concurrency": args.llm_concurrency,
        "workers": args.workers,
    }, args.resume)
    features = FeatureCache(source, symbols, feature_cache_path(opened.name, store.data_version, symbols))
    print(f"feature cache {features.path} ({features.loaded} sessions on disk)", flush=True)
    progress = Progress(out / "progress.jsonl")
    planner = runtime_factory()
    notes = ""
    track: list[dict] = []
    all_rows: list[dict] = []
    proposals: list[dict] = []
    finished = load_finished_rounds(out, rounds) if args.resume else []
    for payload in finished:
        notes = payload["notebook_text"]
        all_rows.extend(payload["rows"])
        track.extend(payload["records"])
        proposals.extend(payload["proposals"])
        progress.log(payload["round"], "round", "skipped", reason="finished before --resume")
    current = ""
    try:
        for spec in rounds[len(finished):]:
            current = spec["round"]
            began = time.perf_counter()
            progress.log(spec["round"], "round", "start", explore=f"{spec['explore_start']}..{spec['explore_end']}",
                         test=f"{spec['test_start']}..{spec['test_end']}")
            nb, explore_rows, round_proposals = run_explore(
                out, store, source, planner, symbols, spec, notes, args.rounds, args.specs, args.workers, track,
                features, progress,
            )
            notes = nb["text"]
            winner = best_explore(explore_rows)
            print(f"best explore spec {winner['spec_id']} net {winner.get('net_profit')}", flush=True)
            test_rows, records = run_test(
                out, store, source, runtime_factory, symbols, spec, nb, winner, args.repeats, args.workers,
                features, progress,
            )
            print_sql(test_rows)
            print_proposals(round_proposals)
            check_round(spec["round"], test_rows)
            all_rows.extend(explore_rows + test_rows)
            track.extend(records)
            proposals.extend(round_proposals)
            write_outputs(out, all_rows, proposals)
            save_round(out, spec, nb, explore_rows + test_rows, records, round_proposals, time.perf_counter() - began)
            progress.log(spec["round"], "round", "done", seconds=round(time.perf_counter() - began, 1),
                         notebook=nb["notebook_id"], cache_fetched=features.fetched, cache_hits=features.hits,
                         reconnects=getattr(opened, "reconnects", 0))
    except (LakeError, RoundFailed) as exc:
        progress.log(current, "round", "failed", error=f"{type(exc).__name__}: {exc}"[:400])
        print(
            f"\nSTOPPED in round {current}: {type(exc).__name__}: {exc}\n"
            f"No round file was written for {current}; finished rounds are kept in {out / 'rounds'}.\n"
            "Fix the lake (docker ps, Test-NetConnection 127.0.0.1 -Port 10000), then rerun the same command with --resume.",
            flush=True,
        )
        raise SystemExit(2) from exc
    finally:
        planner.close()
    write_outputs(out, all_rows, proposals)
    print_table(all_rows)
    print_proposals(proposals)
    print(f"wrote {out / 'results.csv'} and {out / 'proposals.csv'}", flush=True)


def print_sql(rows: list[dict]) -> None:
    for row in rows:
        if "sql_queries" not in row:
            continue
        print(
            f"sql {row['arm']}#{row['repeat']}: {row['sql_ok']}/{row['sql_queries']} queries ran; "
            f"first query ran on {row['sql_first_try_ok']}/{row['sql_decisions']} decisions; "
            f"{row['sql_decisions_answered']}/{row['sql_decisions']} decisions got data; repairs {row['sql_repairs']}"
            + (f"; rules {row['rule_counts']}" if row.get("rule_counts") else ""),
            flush=True,
        )


if __name__ == "__main__":
    main()
