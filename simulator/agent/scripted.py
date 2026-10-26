"""Deterministic test agents.

Every agent implements ``decide(request, feed) -> dict`` and returns the fixed action
envelope. None of them call a model or read anything beyond the released feed.

Script format (JSON):

    {
      "name": "aapl_round_trip",
      "description": "Buy 10 AAPL at the first decision, sell 10 at step 10.",
      "commands": [
        {"step": 0, "orders": [{"symbol": "AAPL", "side": "BUY", "quantity": 10}]},
        {"at": "2025-01-28T14:30:00Z", "orders": [{"symbol": "AAPL", "side": "SELL", "quantity": 10, "offset_seconds": 600}]},
        {"step": 3, "raw": {"anything": "sent verbatim, for rejection tests"}}
      ]
    }

A command matches by ``step`` (0-based decision index) or ``at`` (the request's as_of).
``offset_seconds`` shifts execute_at inside the V2 window; V1 ignores it. Orders default
to MARKET. Steps with no command hold.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from agent.client import FeedReader

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"


class Agent(Protocol):
    name: str

    def decide(self, request: dict, feed: FeedReader) -> Any: ...


def envelope(request: dict, orders: list[dict], reason: str = "") -> dict:
    return {
        "schema_version": "1.0",
        "run_id": request["run_id"],
        "request_id": request["request_id"],
        "mode": request["mode"],
        "window_start": request["window_start"],
        "window_end": request["window_end"],
        "orders": orders,
        "reason": reason,
    }


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def make_order(request: dict, order_id: str, symbol: str, side: str, quantity: int, offset_seconds: int = 0, order_type: str = "MARKET", limit_price: float | None = None) -> dict:
    start = _parse(request["window_start"])
    execute_at = start + timedelta(seconds=offset_seconds) if request["mode"] == "V2" else start
    return {
        "order_id": order_id,
        "execute_at": _iso(execute_at),
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "order_type": order_type,
        "limit_price": limit_price,
    }


class HoldAgent:
    name = "hold"

    def decide(self, request: dict, feed: FeedReader) -> dict:
        return envelope(request, [], "hold")


class CashOnlyAgent:
    """Never trades. Cash earns the simulator's configured rate."""

    name = "cash_only"

    def decide(self, request: dict, feed: FeedReader) -> dict:
        return envelope(request, [], "cash-only: earn interest, do not trade")


class BuyAndHoldAgent:
    """Equal-weight buy of starting cash on day 1, then of each deposit the day it arrives.

    With no deposits this is the original first-decision entry and then hold.
    Respects the published position cap so the control does not depend on rejections.
    """

    name = "buy_and_hold"

    def __init__(self, cash_fraction: float = 0.98):
        self.cash_fraction = cash_fraction

    def decide(self, request: dict, feed: FeedReader) -> dict:
        account = request["account"]
        deposit_today = float(account.get("deposit_today") or 0)
        step = request["step"]
        if step != 0 and deposit_today <= 0:
            return envelope(request, [], "buy-and-hold: holding")
        symbols = request["data"]["symbols"]
        marks = feed.marks()
        cash = float(account["cash"])
        # Day 1 cash already includes that morning's deposit. Later days spend only the new deposit.
        budget = cash if step == 0 else deposit_today
        cap = request["limits"]["max_position_pct_equity"] or 1.0
        per_symbol = min(budget * self.cash_fraction / max(len(symbols), 1), cash * cap * 0.98)
        slip = 1 + request["limits"]["slippage_bps"] / 10_000
        orders = []
        for i, symbol in enumerate(symbols):
            mark = marks.get(symbol, {}).get("close")
            if not mark:
                continue
            qty = math.floor(per_symbol / (mark * slip))
            if qty > 0:
                order_id = f"bh_{i:02d}" if step == 0 else f"bh_{step:05d}_{i:02d}"
                orders.append(make_order(request, order_id, symbol, "BUY", qty))
        if not orders and step != 0:
            return envelope(request, [], "buy-and-hold: deposit does not cover a whole share")
        if step == 0 and deposit_today <= 0:
            reason = "buy-and-hold: equal-weight entry at the first decision"
        elif step == 0:
            reason = "buy-and-hold: equal-weight entry of starting cash and today's deposit"
        else:
            reason = "buy-and-hold: invest today's deposit, equal-weight"
        return envelope(request, orders, reason)


class AccumulatingBuyAndHold(BuyAndHoldAgent):
    """Equal-weight buy-and-hold that keeps unspent cash and later deposits until a whole share fits.

    Day 1 still deploys starting cash. A deposit that cannot buy a share stays in a purse and is
    tried again, with later deposits, on the next session.
    """

    name = "buy_and_hold_accumulate"

    def __init__(self, cash_fraction: float = 0.98):
        super().__init__(cash_fraction)
        self.purse = 0.0

    def decide(self, request: dict, feed: FeedReader) -> dict:
        account = request["account"]
        deposit_today = float(account.get("deposit_today") or 0)
        step = int(request["step"])
        cash = float(account["cash"])
        if step == 0:
            budget = cash * self.cash_fraction
        else:
            self.purse += deposit_today
            budget = self.purse
        if budget <= 0:
            return envelope(request, [], "buy-and-hold: nothing accumulated")
        symbols = request["data"]["symbols"]
        marks = feed.marks()
        cap = request["limits"]["max_position_pct_equity"] or 1.0
        slip = 1 + request["limits"]["slippage_bps"] / 10_000
        n = max(len(symbols), 1)
        per_symbol = min(budget / n, cash * cap * 0.98)
        orders = []
        spent = 0.0
        remaining = cash
        for i, symbol in enumerate(symbols):
            mark = marks.get(symbol, {}).get("close")
            if not mark:
                continue
            unit = mark * slip
            qty = math.floor(min(per_symbol, remaining) / unit)
            if qty <= 0:
                continue
            orders.append(make_order(request, f"bha_{step:05d}_{i:02d}", symbol, "BUY", qty))
            cost = qty * unit
            spent += cost
            remaining -= cost
        leftover = max(0.0, budget - spent)
        self.purse = leftover
        if not orders:
            return envelope(request, [], "buy-and-hold: accumulated cash does not cover a whole share")
        return envelope(request, orders, "buy-and-hold: equal-weight whole shares from accumulated cash")


class ScriptedAgent:
    name = "script"

    def __init__(self, script: dict):
        self.script = script
        self.name = f"script:{script.get('name', 'unnamed')}"
        self._by_step = {c["step"]: c for c in script.get("commands", []) if "step" in c}
        self._by_at = {_parse(c["at"]): c for c in script.get("commands", []) if "at" in c}

    @classmethod
    def from_file(cls, path: str | Path) -> "ScriptedAgent":
        return cls(json.loads(Path(path).read_text()))

    def decide(self, request: dict, feed: FeedReader) -> Any:
        command = self._by_step.get(request["step"]) or self._by_at.get(_parse(request["as_of"]))
        if command is None:
            return envelope(request, [], self.script.get("default_reason", "scripted hold"))
        if "raw" in command:
            return command["raw"]
        orders = []
        for i, spec in enumerate(command.get("orders", [])):
            orders.append(
                make_order(
                    request,
                    spec.get("order_id", f"{request['request_id']}_{i:02d}"),
                    spec["symbol"],
                    spec["side"],
                    int(spec["quantity"]),
                    int(spec.get("offset_seconds", 0)),
                    spec.get("order_type", "MARKET"),
                    spec.get("limit_price"),
                )
            )
        return envelope(request, orders, command.get("reason", "scripted command"))


def list_scripts(directory: Path = SCRIPTS_DIR) -> list[dict]:
    out = []
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text())
        out.append({"id": path.stem, "name": data.get("name", path.stem), "description": data.get("description", ""), "commands": len(data.get("commands", []))})
    return out


def load_agent(kind: str, script: str | None = None) -> Agent:
    if kind == "hold":
        return HoldAgent()
    if kind == "cash_only":
        return CashOnlyAgent()
    if kind == "buy_and_hold":
        return BuyAndHoldAgent()
    if kind == "buy_and_hold_accumulate":
        return AccumulatingBuyAndHold()
    if kind == "script":
        if not script:
            raise ValueError("script agent needs --script <path or id>")
        path = Path(script)
        if not path.exists():
            path = SCRIPTS_DIR / f"{script}.json"
        return ScriptedAgent.from_file(path)
    raise ValueError(f"unknown agent kind {kind!r}; expected hold | cash_only | buy_and_hold | buy_and_hold_accumulate | script")
