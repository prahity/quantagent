"""Build and validate the fixed JSON action envelope on the agent side.

This mirrors the simulator's contract (schema_version 1.0) so the agent can validate its
own response before submitting and fall back to hold on any failure. It deliberately does
NOT import the simulator: the simulator remains the authoritative validator at submit time;
this is a local guard. Keep the rules here in sync with simulator/contracts/action.py.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

SCHEMA_VERSION = "1.0"
_ORDER_KEYS = {"order_id", "execute_at", "symbol", "side", "quantity", "order_type", "limit_price"}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_envelope(request: dict, orders: list[dict], reason: str = "") -> dict:
    """Assemble the response envelope from the request it answers."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": request["run_id"],
        "request_id": request["request_id"],
        "mode": request["mode"],
        "window_start": request["window_start"],
        "window_end": request["window_end"],
        "orders": orders,
        "reason": reason[:2000],
    }


def make_order(
    request: dict,
    order_id: str,
    symbol: str,
    side: str,
    quantity: int,
    offset_seconds: int = 0,
    order_type: str = "MARKET",
    limit_price: float | None = None,
) -> dict:
    """Build a single order. In V2 `offset_seconds` places `execute_at` inside the window;
    in V1 it is ignored (execute_at must equal the boundary)."""
    start = _parse(request["window_start"])
    if request["mode"] == "V2" and offset_seconds:
        from datetime import timedelta

        execute_at = start + timedelta(seconds=offset_seconds)
    else:
        execute_at = start
    return {
        "order_id": order_id,
        "execute_at": _iso(execute_at),
        "symbol": symbol,
        "side": side,
        "quantity": int(quantity),
        "order_type": order_type,
        "limit_price": limit_price,
    }


def validate_envelope(
    payload: object,
    request: dict,
    known_symbols: set[str],
    max_orders: int,
) -> list[str]:
    """Return a list of human-readable errors; empty list means the envelope is valid.

    Any error should make the caller fall back to hold.
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["response must be a single JSON object"]

    required = {"schema_version", "run_id", "request_id", "mode", "window_start", "window_end", "orders"}
    missing = required - set(payload)
    if missing:
        errors.append(f"missing keys: {sorted(missing)}")
        return errors

    if payload["schema_version"] != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    if payload["run_id"] != request["run_id"]:
        errors.append("run_id does not match the request")
    if payload["request_id"] != request["request_id"]:
        errors.append("request_id does not match the request")
    if payload["mode"] != request["mode"]:
        errors.append("mode does not match the request")
    try:
        if _parse(payload["window_start"]) != _parse(request["window_start"]) or _parse(
            payload["window_end"]
        ) != _parse(request["window_end"]):
            errors.append("window_start/window_end must echo the request")
    except (ValueError, TypeError):
        errors.append("window timestamps are not valid ISO-8601")

    orders = payload["orders"]
    if not isinstance(orders, list):
        return errors + ["orders must be a list"]
    if len(orders) > max_orders:
        errors.append(f"{len(orders)} orders exceeds max {max_orders}")

    ws = _parse(request["window_start"])
    we = _parse(request["window_end"])
    seen: set[str] = set()
    for i, order in enumerate(orders):
        tag = f"order[{i}]"
        if not isinstance(order, dict) or set(order) != _ORDER_KEYS:
            errors.append(f"{tag}: keys must be exactly {sorted(_ORDER_KEYS)}")
            continue
        oid = order["order_id"]
        if not isinstance(oid, str) or not (1 <= len(oid) <= 64):
            errors.append(f"{tag}: order_id must be a 1-64 char string")
        if oid in seen:
            errors.append(f"{tag}: duplicate order_id {oid!r}")
        seen.add(oid)
        if order["symbol"] not in known_symbols:
            errors.append(f"{tag}: unknown symbol {order['symbol']!r}")
        if order["side"] not in ("BUY", "SELL"):
            errors.append(f"{tag}: side must be BUY or SELL")
        qty = order["quantity"]
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            errors.append(f"{tag}: quantity must be a positive integer")
        otype = order["order_type"]
        if otype not in ("MARKET", "LIMIT"):
            errors.append(f"{tag}: order_type must be MARKET or LIMIT")
        lp = order["limit_price"]
        if otype == "MARKET" and lp is not None:
            errors.append(f"{tag}: MARKET orders must have limit_price null")
        if otype == "LIMIT" and (not isinstance(lp, (int, float)) or lp is None or lp <= 0 or not math.isfinite(lp)):
            errors.append(f"{tag}: LIMIT orders need a positive finite limit_price")
        try:
            at = _parse(order["execute_at"])
        except (ValueError, TypeError):
            errors.append(f"{tag}: execute_at is not valid ISO-8601")
            continue
        if at.microsecond:
            errors.append(f"{tag}: execute_at must be whole seconds")
        if request["mode"] == "V1" and at != ws:
            errors.append(f"{tag}: V1 execute_at must equal the decision boundary")
        if request["mode"] == "V2" and not (ws <= at < we):
            errors.append(f"{tag}: V2 execute_at must lie in [window_start, window_end)")

    return errors


def hold(request: dict, reason: str = "hold") -> dict:
    """The safe fallback: a valid envelope with no orders."""
    return build_envelope(request, [], reason)
