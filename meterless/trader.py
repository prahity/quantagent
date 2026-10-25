"""One trading decision through Meterless. Nightshift selects the Nemotron lane.

The model sees only the decision request the simulator already released. It must
answer with the fixed action JSON. A reply that is not that JSON becomes a hold,
which the simulator records. The model does not choose the fill price.
"""
from __future__ import annotations

import json
from typing import Any

from meterless.provider import MeterlessRuntime

_PROMPT = """You are the trading agent in a market simulator. Your goal is to maximize net profit by the last step of the run. Open positions are marked at the final close. Decide what to do at this step from the account, the recent completed bars, and the released filings below.
Reply with one JSON object and no other text, in this shape:
{"schema_version":"1.0","run_id":<copy>,"request_id":<copy>,"mode":<copy>,"window_start":<copy>,"window_end":<copy>,"orders":[<order>, ...],"reason":<one or two sentences explaining this decision>}
Copy run_id, request_id, mode, window_start, and window_end from the request unchanged.
orders may be empty, which means hold. Each order is {"order_id":<unique string>,"execute_at":<timestamp>,"symbol":<symbol>,"side":"BUY" or "SELL","quantity":<whole shares>,"order_type":"MARKET" or "LIMIT","limit_price":<number, or null for MARKET>}.
For V1, execute_at must equal window_start. For V2, execute_at must be inside the window.
MARKET orders fill at the open of the next bar. A BUY must fit in cash, no position may exceed max_position_pct_equity of equity, and you can only SELL shares you hold. Size quantity from the latest close in recent_bars.
Use only the symbols, prices, and filings given here. Do not assume anything after as_of.

Request:
"""

_BARS = 10


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
Earlier decisions in this run, oldest first, with the simulator's acknowledgement. Use them to stay consistent and to avoid repeating rejected orders:
"""


class NemotronTrader:
    """Calls Meterless Runtime for each simulator decision request.

    ``memory`` is the number of earlier decisions from this run included in the
    prompt. Zero sends only the current request.
    """

    name = "nemotron"

    def __init__(self, runtime: MeterlessRuntime, memory: int = 0):
        self.runtime = runtime
        self.memory = memory
        self.log: list[dict] = []

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
            "reason": str(payload.get("reason") or "")[:300],
            "accepted": ack.get("accepted"),
            "order_status": [{"status": o.get("status"), "reason": o.get("reason")} for o in ack.get("orders", [])],
            "errors": [e.get("message") for e in ack.get("errors", [])],
        })
        del self.log[: -self.memory]

    def _prompt(self, visible: dict) -> str:
        prompt = _PROMPT + json.dumps(visible)
        if self.memory > 0 and self.log:
            prompt += "\n" + _MEMORY_HEADER + "\n".join(json.dumps(entry) for entry in self.log)
        return prompt

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
        if feed is not None:
            visible.update(_market(request, feed))
        try:
            text = self.runtime.complete(self._prompt(visible))
        except Exception as exc:
            return _hold(request, f"meterless unavailable: {exc}")
        parsed = _json_object(text)
        if parsed is None:
            return _hold(request, "model reply was not the action JSON")
        parsed["reason"] = str(parsed.get("reason") or "nemotron")[:2000]
        return parsed
