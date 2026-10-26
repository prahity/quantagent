"""One trading decision through Meterless. Nightshift selects the Nemotron lane.

The model sees only the decision request the simulator already released. It
answers with orders and a reason. ``structure_action`` copies the request ids
and window onto that reply, and ``fit_orders`` shrinks a buy that would exceed
cash or the position cap. A reply that is not JSON becomes a hold, which the
simulator records. The model does not choose the fill price.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from meterless.action import fit_orders, structure_action
from meterless.provider import MeterlessRuntime
from meterless.query import AnalysisSource, QueryRequest, QueryResult
from meterless.recall import fetch_brief

_REPLY = """Reply with one JSON object and no other text:
{"orders":[{"symbol":"AAPL","side":"BUY","quantity":10,"order_type":"MARKET"}],"reason":"one or two sentences"}
orders may be empty, which means hold. side is BUY or SELL. quantity is the whole shares you choose, sized from the latest close and the cash you want in that symbol. There is no per-symbol size cap. order_type MARKET needs no limit price. A BUY must fit in cash. You can only SELL shares you hold.
Use only the symbols, prices, and filings given here. Do not assume anything after as_of.

Request:
"""

_PROMPT_STYLES = {
    "default": (
        "You are the trading agent in a market simulator. Your goal is to end the window with more cash. "
        "Cash and every open position are counted at the final close of this window. Cash by itself earns nothing. "
        "If you hold no shares and recent closes are higher than earlier ones, buy.\n"
        + _REPLY
    ),
    "deploy": (
        "You are the trading agent in a market simulator. Your goal is to end the window with more cash. "
        "Cash and every open position are counted at the final close of this window. Cash by itself earns nothing. "
        "Idle cash is the main way this run loses to buy-and-hold. If cash is more than about 20% of equity and any "
        "symbol's recent closes are rising, buy enough shares to put most of that cash to work. Split across rising "
        "symbols when more than one is rising. Do not wait for a perfect signal.\n"
        + _REPLY
    ),
    "equal": (
        "You are the trading agent in a market simulator. Your goal is to end the window with more cash. "
        "Cash and every open position are counted at the final close of this window. Cash by itself earns nothing. "
        "When you are fully in cash, or nearly so, buy roughly equal dollar amounts of every tradeable symbol that "
        "has a recent close, using nearly all of the cash. After that, hold unless a clear drop forces a sell.\n"
        + _REPLY
    ),
}

_PROMPT = _PROMPT_STYLES["default"]

_BARS = 10
_QUERY_LIMIT = 1
_MAX_TOKENS = 700
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_SIM_RUNS = Path(__file__).resolve().parents[1] / "simulator" / "runs"

_QUERY_PROMPT = """You are the trading agent. Historical prices and filings are in the SQL relations below, already cut off at this decision's as_of. Use SQL when you need history. Do not estimate a return, a price, or a filing date.
{schema}
Return either {{"goal":"<what you need to know>","sql":"<one SELECT>"}} or the action JSON from the trading instructions.
The simulator's as_of is fixed. Putting a later time in the SQL does not reveal it.

Trading instructions:
"""


def _json_object(text: str) -> dict[str, Any] | None:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _closes(visible: dict) -> dict[str, float]:
    prices = {}
    for symbol, bars in (visible.get("recent_bars") or {}).items():
        if bars:
            prices[symbol] = float(bars[-1]["close"])
    return prices


def _ready(parsed: dict, request: dict, visible: dict) -> tuple[dict, list[dict]]:
    """Return the repaired action and the orders the model actually asked for."""
    structured = structure_action(parsed, request)
    original = [dict(order) for order in structured["orders"]]
    return fit_orders(structured, request, _closes(visible)), original


def _hold(request: dict, reason: str) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": request["run_id"],
        "request_id": request["request_id"],
        "mode": request["mode"],
        "window_start": request["window_start"],
        "window_end": request["window_end"],
        "orders": [],
        "reason": reason[:2000],
    }


def _market(request: dict, feed: Any) -> dict:
    """Released bars and filings from the simulator feed. The feed refuses anything after as_of."""
    symbols = (request.get("data") or {}).get("symbols") or []
    bars = {}
    for symbol in symbols:
        bars[symbol] = [
            {
                "start": str(b["ts"])[:16],
                **{k: round(float(b[k]), 2) for k in ("open", "high", "low", "close")},
                "volume": int(b["volume"]),
            }
            for b in feed.prices(symbol, _BARS)
        ]
    wanted = set((request.get("data") or {}).get("news_event_ids") or [])
    filings = [
        {"symbol": n["symbol"], "published_at": str(n["published_at"])[:16], "title": n["title"]}
        for n in feed.news(symbols) if n["event_id"] in wanted
    ]
    return {"recent_bars": bars, "released_filings": filings}


_MEMORY_HEADER = """
Earlier orders in this run, oldest first, with the simulator's acknowledgement. Use them to avoid repeating rejected orders. Decide each step afresh from the current data:
"""

_NOTES_HEADER = """
Strategy notebook. You wrote it after reviewing your results in earlier, completed periods. Apply its rules where they fit the current data:
"""


class NemotronTrader:
    """Calls Meterless Runtime for each simulator decision request.

    ``memory`` is the number of earlier orders from this run included in the
    prompt. ``notes`` is a strategy notebook carried in from earlier runs. With
    both off, each decision sees only the current request and feed.
    """

    name = "nemotron"

    def __init__(
        self,
        runtime: MeterlessRuntime,
        memory: int = 0,
        notes: str = "",
        retries: int = 0,
        source: AnalysisSource | None = None,
        recall: str = "sql",
        prompt_style: str = "default",
        log_dir: str | Path | None = None,
    ):
        self.runtime = runtime
        self.memory = memory
        self.notes = notes
        self.retries = retries
        self.source = source
        self.recall = recall
        self.prompt_style = prompt_style if prompt_style in _PROMPT_STYLES else "default"
        self.log_dir = Path(log_dir) if log_dir else None
        self.log: list[dict] = []
        self.queries: list[QueryResult] = []
        self.repairs: list[dict] = []

    def _trade_prompt(self) -> str:
        return _PROMPT_STYLES[self.prompt_style]

    def observe(self, request: dict, payload: dict, ack: dict) -> None:
        if self.memory <= 0:
            return
        self.log.append({
            "step": request.get("step"),
            "as_of": request.get("as_of"),
            "orders": [
                {k: o.get(k) for k in ("symbol", "side", "quantity", "order_type", "limit_price")}
                for o in payload.get("orders") or [] if isinstance(o, dict)
            ],
            "accepted": ack.get("accepted"),
            "order_status": [{"status": o.get("status"), "reason": o.get("reason")} for o in ack.get("orders", [])],
            "errors": [e.get("message") for e in ack.get("errors", [])],
        })
        del self.log[: -self.memory]

    def _prompt(self, visible: dict) -> str:
        prompt = self._trade_prompt() + json.dumps(visible)
        if self.notes:
            prompt += "\n" + _NOTES_HEADER + self.notes
        if self.memory > 0 and self.log:
            prompt += "\n" + _MEMORY_HEADER + "\n".join(json.dumps(entry) for entry in self.log)
        return prompt

    def _visible(self, request: dict, feed: Any) -> dict:
        visible = {
            "schema_version": request.get("schema_version"),
            "run_id": request.get("run_id"),
            "request_id": request.get("request_id"),
            "mode": request.get("mode"),
            "as_of": request.get("as_of"),
            "window_start": request.get("window_start"),
            "window_end": request.get("window_end"),
            "account": request.get("account"),
            "data": request.get("data"),
            "limits": request.get("limits"),
            "step": request.get("step"),
            "steps_total": request.get("steps_total"),
        }
        if feed is not None:
            visible.update(_market(request, feed))
        return visible

    def _log_root(self, request: dict) -> Path:
        if self.log_dir is not None:
            return self.log_dir
        return _SIM_RUNS / str(request.get("run_id") or "unknown")

    def _append_log(self, request: dict, name: str, record: dict) -> None:
        path = self._log_root(request) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def _log_model_call(self, request: dict, attempt: int, error: str | None) -> None:
        recorded = getattr(self.runtime, "last_completion", None)
        self._append_log(request, "model_calls.jsonl", {
            "request_id": request.get("request_id"),
            "as_of": request.get("as_of"),
            "attempt": attempt,
            "status": getattr(recorded, "status", None),
            "latency_ms": getattr(recorded, "latency_ms", None),
            "finish_reason": getattr(recorded, "finish_reason", None),
            "prompt_tokens": getattr(recorded, "prompt_tokens", None),
            "completion_tokens": getattr(recorded, "completion_tokens", None),
            "total_tokens": getattr(recorded, "total_tokens", None),
            "error": error or getattr(recorded, "error", None),
        })

    def _complete(self, prompt: str, request: dict) -> str | dict:
        for attempt in range(self.retries + 1):
            try:
                text = self.runtime.complete(prompt, max_tokens=_MAX_TOKENS)
            except Exception as exc:
                self._log_model_call(request, attempt, type(exc).__name__)
                if attempt == self.retries:
                    return _hold(request, f"meterless unavailable: {exc}")
                time.sleep(15 * (attempt + 1))
                continue
            self._log_model_call(request, attempt, None)
            return text
        return _hold(request, "meterless unavailable")

    def _query_prompt(self, visible: dict, found: list[QueryResult]) -> str:
        prompt = _QUERY_PROMPT.format(schema=self.source.schema_text) + self._trade_prompt() + json.dumps(visible)
        if found:
            prompt += "\nSQL results for this decision. Cite these figures. Do not replace them:\n"
            prompt += "\n".join(f"{item.query_id} {item.goal}\n{item.text}" for item in found)
        if self.notes:
            prompt += "\n" + _NOTES_HEADER + self.notes
        if self.memory > 0 and self.log:
            prompt += "\n" + _MEMORY_HEADER + "\n".join(json.dumps(entry) for entry in self.log)
        return prompt

    def _ground_reason(self, reason: str, visible: dict, found: list[QueryResult]) -> tuple[str, bool]:
        """Keep the model's reason. Flag it when it cites a number that was not in the evidence.

        The flag is recorded beside the order repair. It is not added to the action:
        the simulator rejects any key outside the fixed envelope.
        """
        original = (reason or "")[:2000]
        sources = json.dumps(visible) + "\n" + "\n".join(item.text for item in found)
        unsupported = not set(_NUMBER.findall(original)) <= set(_NUMBER.findall(sources))
        return original, unsupported

    def _apply(self, parsed: dict, request: dict, visible: dict, found: list[QueryResult] | None) -> dict:
        action, original_orders = _ready(parsed, request, visible)
        unsupported = None
        if found is not None:
            action["reason"], unsupported = self._ground_reason(action.get("reason") or "", visible, found)
        record = {
            "request_id": request.get("request_id"),
            "as_of": request.get("as_of"),
            "original_orders": original_orders,
            "repaired_orders": action.get("orders"),
            "reason": action.get("reason"),
            "reason_unsupported": unsupported,
        }
        self.repairs.append(record)
        self._append_log(request, "order_repairs.jsonl", record)
        return action

    def decide(self, request: dict, feed: Any = None) -> dict:
        visible = self._visible(request, feed)
        if self.recall == "brief" and self.source is not None:
            brief, found = fetch_brief(self.source, request)
            self.queries.extend(found)
            text = self._complete(self._prompt(visible) + "\n" + brief, request)
            if isinstance(text, dict):
                return text
            parsed = _json_object(text)
            if parsed is None or "orders" not in parsed:
                return _hold(request, "model reply was not the action JSON")
            return self._apply(parsed, request, visible, found)
        if self.source is None or self.recall == "notes":
            text = self._complete(self._prompt(visible), request)
            if isinstance(text, dict):
                return text
            parsed = _json_object(text)
            if parsed is None:
                return _hold(request, "model reply was not the action JSON")
            return self._apply(parsed, request, visible, None)

        found: list[QueryResult] = []
        for _ in range(_QUERY_LIMIT):
            text = self._complete(self._query_prompt(visible, found), request)
            if isinstance(text, dict):
                return text
            parsed = _json_object(text) or {}
            sql = parsed.get("sql")
            if isinstance(sql, str) and sql.strip():
                result = self.source.execute(QueryRequest(
                    run_id=str(request.get("run_id") or ""),
                    request_id=str(request.get("request_id") or ""),
                    query_id=f"{request.get('request_id')}-q{len(self.queries) + 1}",
                    goal=str(parsed.get("goal") or "")[:300],
                    sql=sql,
                    as_of=str(request.get("as_of") or ""),
                ))
                self.queries.append(result)
                found.append(result)
                continue
            if "orders" in parsed:
                return self._apply(parsed, request, visible, found)
            break
        text = self._complete(self._query_prompt(visible, found) + "\nReply with the action JSON only.", request)
        if isinstance(text, dict):
            return text
        parsed = _json_object(text)
        if parsed is None or "orders" not in parsed:
            return _hold(request, "model reply was not the action JSON")
        return self._apply(parsed, request, visible, found)
