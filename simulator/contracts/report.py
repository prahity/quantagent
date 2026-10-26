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
    net_profit: float = Field(description="final_equity - initial_cash - total_deposits. Interest stays inside equity.")
    return_pct: float = Field(description="Simple final_equity / initial_cash - 1, in percent. Not deposit-adjusted.")
    max_drawdown_pct: float = Field(description="Drawdown of equity minus deposits credited so far. Equals raw-equity drawdown when there are no deposits.")
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
    total_deposits: float = 0
    interest_earned: float = 0
    time_weighted_return_pct: float = Field(default=0, description="Return that strips deposits. Equals return_pct when total_deposits is 0.")
    daily_deposit: float = 0
    cash_annual_rate: float = 0
    daily_cash: list[dict] = Field(default_factory=list, description="One row per trading day: deposit, interest, and cash left uninvested.")
