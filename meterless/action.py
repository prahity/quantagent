"""Turn a model reply into the simulator's action JSON.

The simulator rejects the whole reply when a field is extra or an id does not
match. This step keeps the orders, fills the envelope from the request, and
shrinks a buy that would exceed cash or the position cap.
"""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Order(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: str | None = None
    execute_at: str | None = None
    symbol: str
    side: str
    quantity: int = Field(gt=0)
    order_type: str = "MARKET"
    limit_price: float | None = None

    @field_validator("quantity", mode="before")
    @classmethod
    def whole_shares(cls, value: Any) -> int:
        return int(float(value))

    @field_validator("side", "order_type", mode="before")
    @classmethod
    def upper(cls, value: Any) -> str:
        return str(value).strip().upper()

    @field_validator("symbol", mode="before")
    @classmethod
    def ticker(cls, value: Any) -> str:
        return str(value).strip().upper()


class _Action(BaseModel):
    model_config = ConfigDict(extra="ignore")

    orders: list[_Order] = Field(default_factory=list)
    reason: str = ""

    @field_validator("orders", mode="before")
    @classmethod
    def drop_bad_orders(cls, value: Any) -> list:
        if not isinstance(value, list):
            return []
        kept = []
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                kept.append(_Order.model_validate(item))
            except ValueError:
                continue
        return kept


def structure_action(payload: dict, request: dict) -> dict:
    """Copy ids and the window from the request. Keep only valid orders."""
    parsed = _Action.model_validate(payload if isinstance(payload, dict) else {})
    window = str(request["window_start"])
    orders = []
    seen: set[str] = set()
    for index, order in enumerate(parsed.orders):
        if order.side not in ("BUY", "SELL"):
            continue
        order_type = order.order_type if order.order_type in ("MARKET", "LIMIT") else "MARKET"
        if order_type == "LIMIT" and (order.limit_price is None or order.limit_price <= 0):
            continue
        order_id = (order.order_id or f"{request['request_id']}-{index}")[:64]
        if order_id in seen:
            order_id = f"{order_id}-{index}"[:64]
        seen.add(order_id)
        orders.append({
            "order_id": order_id,
            "execute_at": window if request.get("mode") == "V1" else (order.execute_at or window),
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "order_type": order_type,
            "limit_price": None if order_type == "MARKET" else order.limit_price,
        })
    return {
        "schema_version": "1.0",
        "run_id": request["run_id"],
        "request_id": request["request_id"],
        "mode": request["mode"],
        "window_start": request["window_start"],
        "window_end": request["window_end"],
        "orders": orders,
        "reason": (parsed.reason or "nemotron")[:2000],
    }


def fit_orders(action: dict, request: dict, prices: dict[str, float]) -> dict:
    """Shrink each order to shares the published cash and position cap can hold.

    The next open is unknown, so size from the latest completed close with a
    small cushion for slippage. An order that shrinks to zero is dropped.
    """
    account = request.get("account") or {}
    limits = request.get("limits") or {}
    cash = float(account.get("cash") or 0)
    equity = float(account.get("equity") or cash)
    cap = float(limits.get("max_position_pct_equity") or 0)
    slip = float(limits.get("slippage_bps") or 0) / 10_000
    held: dict[str, int] = {}
    marks = dict(prices)
    for position in account.get("positions") or []:
        held[str(position.get("symbol"))] = int(position.get("quantity") or 0)
        mark = position.get("mark_price")
        if mark:
            marks.setdefault(str(position.get("symbol")), float(mark))

    fitted = []
    for order in action.get("orders") or []:
        price = marks.get(order["symbol"])
        if not price or price <= 0:
            continue
        qty = int(order["quantity"])
        if order["side"] == "BUY":
            affordable = math.floor(cash * 0.98 / (price * (1 + slip)))
            room = affordable
            if cap and equity > 0:
                room = min(room, math.floor(cap * equity * 0.98 / price) - held.get(order["symbol"], 0))
            qty = min(qty, max(room, 0))
            if qty < 1:
                continue
            cash -= qty * price * (1 + slip)
            held[order["symbol"]] = held.get(order["symbol"], 0) + qty
        else:
            qty = min(qty, held.get(order["symbol"], 0))
            if qty < 1:
                continue
            held[order["symbol"]] -= qty
            cash += qty * price * (1 - slip)
        fitted.append({**order, "quantity": qty})
    return {**action, "orders": fitted}
