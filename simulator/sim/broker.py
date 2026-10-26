"""Order execution and the account ledger. The only place cash and positions change."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from contracts.action import Order
from contracts.report import Fill, Rejection


@dataclass
class PositionState:
    quantity: int = 0
    avg_cost: float = 0.0


@dataclass
class CashEvent:
    """A simulator-owned cash credit. The agent cannot create either kind."""

    kind: str  # "deposit" or "interest"
    at: datetime
    trading_day: str
    amount: float
    cash_after: float


@dataclass
class ExecutionConfig:
    commission_per_share: float = 0.0
    commission_min: float = 0.0
    slippage_bps: float = 1.0
    allow_shorting: bool = False
    max_position_pct_equity: float = 0.25
    max_orders_per_request: int = 20
    order_validity_bars: int = 1
    fill_price: str = "next_bar_open"

    @classmethod
    def from_dict(cls, raw: dict) -> "ExecutionConfig":
        known = {k: v for k, v in (raw or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Ledger:
    initial_cash: float
    execution: ExecutionConfig
    cash: float = field(init=False)
    positions: dict[str, PositionState] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    total_deposits: float = 0.0
    interest_earned: float = 0.0
    cash_events: list[CashEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = float(self.initial_cash)

    def credit(self, kind: str, amount: float, at: pd.Timestamp, trading_day: str) -> CashEvent:
        """Add cash the simulator owns. ``kind`` is ``deposit`` or ``interest``."""
        if kind not in ("deposit", "interest"):
            raise ValueError(f"unknown cash credit {kind!r}")
        self.cash += float(amount)
        if kind == "deposit":
            self.total_deposits += float(amount)
        else:
            self.interest_earned += float(amount)
        event = CashEvent(
            kind=kind,
            at=at.to_pydatetime(),
            trading_day=trading_day,
            amount=float(amount),
            cash_after=self.cash,
        )
        self.cash_events.append(event)
        return event

    def held_symbols(self) -> list[str]:
        return [s for s, p in self.positions.items() if p.quantity != 0]

    def equity(self, marks: dict[str, float]) -> float:
        value = self.cash
        for symbol, pos in self.positions.items():
            if pos.quantity and symbol in marks:
                value += pos.quantity * marks[symbol]
        return value

    def position_rows(self, marks: dict[str, float]) -> list[dict]:
        rows = []
        for symbol, pos in sorted(self.positions.items()):
            if pos.quantity == 0:
                continue
            mark = marks.get(symbol)
            rows.append(
                {
                    "symbol": symbol,
                    "quantity": pos.quantity,
                    "avg_cost": round(pos.avg_cost, 6),
                    "mark_price": mark,
                    "market_value": None if mark is None else round(pos.quantity * mark, 6),
                }
            )
        return rows

    def _commission(self, quantity: int) -> float:
        return max(self.execution.commission_per_share * quantity, self.execution.commission_min)

    def execute(self, order: Order, request_id: str, bar_open: float, filled_at: pd.Timestamp, marks: dict[str, float]) -> Fill | Rejection:
        slip = self.execution.slippage_bps / 10_000.0
        if order.order_type == "LIMIT":
            # Marketable at the open only; otherwise it waits for a later bar or expires.
            # Not recorded as a final rejection: the scheduler keeps the order pending.
            if order.side == "BUY" and bar_open > order.limit_price:
                return self._reject(order, request_id, filled_at, "limit_not_marketable", record=False)
            if order.side == "SELL" and bar_open < order.limit_price:
                return self._reject(order, request_id, filled_at, "limit_not_marketable", record=False)

        price = bar_open * (1 + slip) if order.side == "BUY" else bar_open * (1 - slip)
        commission = self._commission(order.quantity)
        notional = price * order.quantity
        pos = self.positions.setdefault(order.symbol, PositionState())

        if order.side == "BUY":
            if notional + commission > self.cash + 1e-9:
                return self._reject(order, request_id, filled_at, "insufficient_cash")
            new_qty = pos.quantity + order.quantity
        else:
            if not self.execution.allow_shorting and order.quantity > pos.quantity:
                return self._reject(order, request_id, filled_at, "insufficient_inventory")
            new_qty = pos.quantity - order.quantity

        equity = self.equity({**marks, order.symbol: bar_open})
        cap = self.execution.max_position_pct_equity
        if cap and abs(new_qty) * bar_open > cap * equity + 1e-9 and abs(new_qty) > abs(pos.quantity):
            return self._reject(order, request_id, filled_at, "position_limit")

        if order.side == "BUY":
            total_cost = pos.avg_cost * pos.quantity + price * order.quantity
            pos.avg_cost = total_cost / new_qty if new_qty else 0.0
            self.cash -= notional + commission
        else:
            if new_qty == 0:
                pos.avg_cost = 0.0
            self.cash += notional - commission
        pos.quantity = new_qty

        fill = Fill(
            order_id=order.order_id,
            request_id=request_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            execute_at=order.execute_at,
            filled_at=filled_at.to_pydatetime(),
            bar_open=bar_open,
            fill_price=price,
            commission=commission,
            slippage_cost=abs(price - bar_open) * order.quantity,
            notional=notional,
            cash_after=self.cash,
        )
        self.fills.append(fill)
        return fill

    def reject(self, order: Order, request_id: str, at: pd.Timestamp, reason: str) -> Rejection:
        return self._reject(order, request_id, at, reason)

    def _reject(self, order: Order, request_id: str, at: pd.Timestamp, reason: str, record: bool = True) -> Rejection:
        rejection = Rejection(
            order_id=order.order_id,
            request_id=request_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            execute_at=order.execute_at,
            at=at.to_pydatetime(),
            reason=reason,
        )
        if record:
            self.rejections.append(rejection)
        return rejection
