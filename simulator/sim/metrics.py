"""Official metrics from ledger records only."""
from __future__ import annotations

import math

import numpy as np

from contracts.report import EquityPoint, Fill


def max_drawdown_pct(equity: list[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst * 100.0


def sharpe(equity: list[float], periods_per_year: int) -> float | None:
    if len(equity) < 3:
        return None
    arr = np.asarray(equity, dtype=float)
    rets = np.diff(arr) / arr[:-1]
    if rets.std(ddof=1) == 0 or not np.isfinite(rets.std(ddof=1)):
        return None
    return float(rets.mean() / rets.std(ddof=1) * math.sqrt(periods_per_year))


def with_drawdown(points: list[tuple]) -> list[EquityPoint]:
    out: list[EquityPoint] = []
    peak = -math.inf
    for ts, cash, equity in points:
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        out.append(EquityPoint(ts=ts, cash=cash, equity=equity, drawdown=dd))
    return out


def trade_totals(fills: list[Fill]) -> dict:
    return {
        "traded_notional": float(sum(f.notional for f in fills)),
        "commissions": float(sum(f.commission for f in fills)),
        "slippage_cost": float(sum(f.slippage_cost for f in fills)),
    }
