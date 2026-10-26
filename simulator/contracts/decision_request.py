from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.1"


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    quantity: int
    avg_cost: float
    mark_price: float | None = Field(default=None, description="Last eligible close at as_of; null if none yet")
    market_value: float | None = None


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = "USD"
    cash: float
    equity: float
    positions: list[Position] = Field(default_factory=list)
    deposit_today: float = Field(default=0, description="Cash credited at the start of this trading day. Non-zero only on the first decision of that day, and already included in cash.")
    total_deposits: float = Field(default=0, description="Sum of daily deposits so far. Does not include initial cash.")
    interest_earned: float = Field(default=0, description="Cash interest credited so far.")
    cash_annual_rate: float = Field(default=0, description="Fixed annual rate on uninvested cash. Each trading day credits cash * rate / 252.")


class Clock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cadence_clock: Literal["simulated"] = "simulated"
    decision_interval_seconds: int
    time_scale: float
    execution_resolution_ms: int
    interval: Literal["hourly", "daily"]


class DataRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feed_schema: str
    workspace_schema: str
    news_event_ids: list[str] = Field(default_factory=list, description="Relevance digest of eligible news/context records")
    symbols: list[str] = Field(default_factory=list, description="Symbols that accept orders in this run")


class Limits(BaseModel):
    """Execution rules the simulator will enforce; published so agents can size orders honestly."""

    model_config = ConfigDict(extra="forbid")

    max_orders_per_request: int
    max_position_pct_equity: float
    allow_shorting: bool
    order_validity_bars: int
    slippage_bps: float
    commission_per_share: float


class DecisionRequest(BaseModel):
    """What the simulator sends at every decision boundary. The agent sees only data released by ``as_of``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.1"] = SCHEMA_VERSION
    run_id: str
    request_id: str
    mode: Literal["V1", "V2"]
    as_of: datetime
    window_start: datetime
    window_end: datetime
    initial_cash: float
    account: Account
    clock: Clock
    data: DataRefs
    limits: Limits
    step: int = Field(description="0-based decision index within the run")
    steps_total: int
