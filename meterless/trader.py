"""One trading decision through Meterless. Nightshift selects the Nemotron lane.

The model sees only the decision request the simulator already released. It must
answer with the fixed action JSON. A reply that is not that JSON becomes a hold,
which the simulator records. The model does not choose the fill price.
"""
from __future__ import annotations

import json
from typing import Any

from meterless.provider import MeterlessRuntime

_PROMPT = """You are the trading agent for one simulator decision. Reply with one JSON object and no other text.
The object must match this shape:
{"schema_version":"1.0","run_id":"...","request_id":"...","mode":"V1 or V2","window_start":"...","window_end":"...","orders":[],"reason":"short"}
Copy run_id, request_id, mode, window_start, and window_end from the request unchanged.
orders is an array. An empty array means hold. Each order has order_id, execute_at, symbol, side (BUY or SELL), quantity, order_type (MARKET or LIMIT), and limit_price (null for MARKET).
For V1, execute_at must equal window_start. For V2, execute_at must be inside the window.
Use only symbols, prices, and news in the request. Do not assume anything after as_of.

Request:
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


class NemotronTrader:
    """Calls Meterless Runtime for each simulator decision request."""

    name = "nemotron"

    def __init__(self, runtime: MeterlessRuntime):
        self.runtime = runtime

    def decide(self, request: dict, feed: Any = None) -> dict:
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
        try:
            text = self.runtime.complete(_PROMPT + json.dumps(visible))
        except Exception as exc:
            return _hold(request, f"meterless unavailable: {exc}")
        parsed = _json_object(text)
        if parsed is None:
            return _hold(request, "model reply was not the action JSON")
        parsed["reason"] = str(parsed.get("reason") or "nemotron")[:2000]
        return parsed
