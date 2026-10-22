from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Fill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    request_id: str
    symbol: str
    side: str
    quantity: int
    execute_at: datetime = Field(description="Activation time chosen by the agent")
    filled_at: datetime = Field(description="Bar start whose open was used")
    bar_open: float
    fill_price: float = Field(description="bar_open adjusted for slippage")
    commission: float
    slippage_cost: float
    notional: float
    cash_after: float


class Rejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    request_id: str
    symbol: str
    side: str
    quantity: int
    execute_at: datetime
    at: datetime
    reason: str


class EquityPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: datetime
    cash: float
    equity: float
    drawdown: float


class Report(BaseModel):
    """Official end-of-run report. Produced from the ledger only."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    run_id: str
    label: str | None = None
    environment_version: str
    data_version: str
    interval: str
    mode: str
    symbols: list[str]
    start: datetime
    end: datetime
    steps: int
    initial_cash: float
    final_cash: float
    final_equity: float
    net_profit: float
    return_pct: float
    max_drawdown_pct: float
    sharpe: float | None
    sharpe_periods_per_year: int
    turnover: float = Field(description="Sum of traded notional divided by initial_cash")
    traded_notional: float
    commissions: float
    slippage_cost: float
    fills: int
    rejections: int
    invalid_responses: int
    held_requests: int
    final_positions: list[dict]
    valuation_convention: str
    costs: dict
