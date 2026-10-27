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


def excess_sharpe(daily_growth: list[float], cash_annual_rate: float, periods_per_year: int = 252) -> float | None:
    """Sharpe of daily time-weighted growth minus the daily cash rate, annualized with sqrt(252).

    Each growth factor is day-end equity divided by equity after that day's deposit.
    A cash-only account grows by exactly the cash rate, so its excess is 0.
    """
    if len(daily_growth) < 2:
        return None
    daily_rate = float(cash_annual_rate) / periods_per_year
    excess = np.asarray([float(factor) - 1.0 - daily_rate for factor in daily_growth], dtype=float)
    if not np.isfinite(excess).all():
        return None
    excess = np.where(np.abs(excess) < 1e-12, 0.0, excess)
    std = float(excess.std(ddof=1))
    mean = float(excess.mean())
    if not math.isfinite(std) or std == 0.0:
        return 0.0 if abs(mean) <= 1e-12 else None
    return mean / std * math.sqrt(periods_per_year)


def sharpe(equity: list[float], periods_per_year: int) -> float | None:
    """Legacy per-bar Sharpe. Reports use excess_sharpe on daily time-weighted returns."""
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
