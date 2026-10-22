from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError

from contracts.decision_request import SCHEMA_VERSION, DecisionRequest


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1, max_length=64)
    execute_at: datetime
    symbol: str = Field(min_length=1, max_length=12)
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    order_type: Literal["MARKET", "LIMIT"]
    limit_price: float | None = None


class ActionResponse(BaseModel):
    """Single fixed envelope for V1 and V2. ``orders: []`` means hold."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str
    request_id: str
    mode: Literal["V1", "V2"]
    window_start: datetime
    window_end: datetime
    orders: list[Order] = Field(default_factory=list)
    reason: str = Field(default="", max_length=2000)


class ValidationError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    order_id: str | None = None


class OrderOutcome(BaseModel):
    """Per-order status returned in the acknowledgement and updated as execution proceeds."""

    model_config = ConfigDict(extra="forbid")

    order_id: str
    status: Literal["accepted", "rejected"]
    reason: str | None = None


class Acknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str
    request_id: str
    accepted: bool = Field(description="False when the whole response was rejected and treated as hold")
    errors: list[ValidationError] = Field(default_factory=list)
    orders: list[OrderOutcome] = Field(default_factory=list)


def _utc(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def validate_action(
    payload: Any,
    request: DecisionRequest,
    known_symbols: set[str],
    max_orders: int,
) -> tuple[ActionResponse | None, list[ValidationError]]:
    """Validate an agent response against the request it answers.

    Returns the parsed response and an empty error list, or ``None`` plus the errors.
    Any error rejects the whole response; the simulator then records a hold.
    """
    errors: list[ValidationError] = []
    if not isinstance(payload, dict):
        return None, [ValidationError(code="not_an_object", message="response must be a single JSON object")]

    try:
        response = ActionResponse.model_validate(payload)
    except PydanticValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            errors.append(ValidationError(code="schema", message=f"{loc}: {err['msg']}"))
        return None, errors

    if response.run_id != request.run_id:
        errors.append(ValidationError(code="run_mismatch", message=f"run_id {response.run_id!r} != {request.run_id!r}"))
    if response.request_id != request.request_id:
        errors.append(ValidationError(code="request_mismatch", message=f"request_id {response.request_id!r} != {request.request_id!r}"))
    if response.mode != request.mode:
        errors.append(ValidationError(code="mode_mismatch", message=f"mode {response.mode} != {request.mode}"))
    if _utc(response.window_start) != _utc(request.window_start) or _utc(response.window_end) != _utc(request.window_end):
        errors.append(ValidationError(code="window_mismatch", message="window_start/window_end must echo the request"))
    if len(response.orders) > max_orders:
        errors.append(ValidationError(code="too_many_orders", message=f"{len(response.orders)} orders > max {max_orders}"))

    seen: set[str] = set()
    ws, we = _utc(request.window_start), _utc(request.window_end)
    for order in response.orders:
        if order.order_id in seen:
            errors.append(ValidationError(code="duplicate_order_id", message="order_id repeated in response", order_id=order.order_id))
        seen.add(order.order_id)
        if order.symbol not in known_symbols:
            errors.append(ValidationError(code="unknown_symbol", message=f"{order.symbol} is not in this run's universe", order_id=order.order_id))
        if order.limit_price is not None and not math.isfinite(order.limit_price):
            errors.append(ValidationError(code="bad_limit_price", message="limit_price must be finite", order_id=order.order_id))
        if order.order_type == "MARKET" and order.limit_price is not None:
            errors.append(ValidationError(code="bad_limit_price", message="MARKET orders must have limit_price null", order_id=order.order_id))
        if order.order_type == "LIMIT" and (order.limit_price is None or order.limit_price <= 0):
            errors.append(ValidationError(code="bad_limit_price", message="LIMIT orders need a positive limit_price", order_id=order.order_id))
        if order.execute_at.microsecond:
            errors.append(ValidationError(code="timestamp_precision", message="execute_at must be whole seconds", order_id=order.order_id))
        at = _utc(order.execute_at)
        if request.mode == "V1" and at != ws:
            errors.append(ValidationError(code="v1_timing", message="V1 execute_at must equal the decision boundary", order_id=order.order_id))
        if request.mode == "V2" and not (ws <= at < we):
            errors.append(ValidationError(code="v2_timing", message="V2 execute_at must lie in [window_start, window_end)", order_id=order.order_id))

    if errors:
        return None, errors
    return response, []
