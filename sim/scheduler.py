"""Queue of accepted orders waiting for their activation time and a market event."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from contracts.action import Order


@dataclass
class PendingOrder:
    execute_at_ns: int
    seq: int
    order: Order
    request_id: str
    placed_step: int


class Scheduler:
    def __init__(self, validity_bars: int):
        self.validity_bars = validity_bars
        self._pending: list[PendingOrder] = []
        self._seq = 0

    def add(self, order: Order, request_id: str, placed_step: int) -> None:
        self._seq += 1
        execute_at = pd.Timestamp(order.execute_at)
        execute_at = execute_at.tz_convert("UTC") if execute_at.tzinfo else execute_at.tz_localize("UTC")
        self._pending.append(PendingOrder(int(execute_at.value), self._seq, order, request_id, placed_step))
        self._pending.sort(key=lambda p: (p.execute_at_ns, p.seq))

    def due(self, t: pd.Timestamp) -> list[PendingOrder]:
        t_ns = int(t.value)
        return [p for p in self._pending if p.execute_at_ns <= t_ns]

    def expired(self, current_step: int) -> list[PendingOrder]:
        """Orders that already had their last permitted fill attempt at ``current_step``."""
        return [p for p in self._pending if current_step >= p.placed_step + self.validity_bars]

    def remove(self, pending: PendingOrder) -> None:
        self._pending = [p for p in self._pending if p is not pending]

    def drain(self) -> list[PendingOrder]:
        out, self._pending = self._pending, []
        return out

    def __len__(self) -> int:
        return len(self._pending)

    def rows(self) -> list[dict]:
        return [
            {
                "order_id": p.order.order_id,
                "symbol": p.order.symbol,
                "side": p.order.side,
                "quantity": p.order.quantity,
                "execute_at": pd.Timestamp(p.execute_at_ns, unit="ns", tz="UTC").isoformat(),
                "request_id": p.request_id,
                "placed_step": p.placed_step,
            }
            for p in self._pending
        ]
