"""Deterministic rule runner. It never calls a model.

Features are whatever the caller already computed at this decision's as_of.
The same spec and the same feature rows always produce the same orders.
"""
from __future__ import annotations

import math
from typing import Any, Callable

from agent.scripted import envelope, make_order

Features = Callable[[str], list[dict]]


def _score(row: dict, kind: str, lookback: int) -> float | None:
    ret_col = {5: "ret_5", 21: "ret_21", 63: "ret_63"}[lookback]
    if kind == "momentum":
        return row.get(ret_col)
    if kind == "mean_reversion":
        if lookback == 5:
            value = row.get("ret_5")
            return None if value is None else -float(value)
        gap = row.get("ma_gap_21" if lookback == 21 else "ma_gap_63")
        return None if gap is None else -float(gap)
    if kind == "volatility":
        value = row.get("vol_21")
        return None if value is None else -float(value)
    if kind == "relative_strength":
        return row.get("rs_spy")
    if kind == "filing_event":
        return row.get(ret_col)
    return None


def _targets(spec: dict, rows: list[dict], tradable: list[str]) -> dict[str, float]:
    signal = spec["signal"]
    selection = spec["selection"]
    universe = set(spec.get("universe") or tradable)
    ranked: list[tuple[float, str]] = []
    for row in rows:
        symbol = row.get("symbol")
        if symbol not in universe or symbol not in tradable:
            continue
        limit = signal.get("max_days_since_filing")
        if limit is not None:
            days = row.get("days_since_filing")
            if days is None or int(days) > int(limit):
                continue
        forms = [str(item).upper() for item in (signal.get("forms") or [])]
        form = str(row.get("form") or "").upper()
        if forms and form and form not in forms:
            continue
        value = _score(row, signal["kind"], int(signal["lookback"]))
        if value is None:
            continue
        ranked.append((float(value), symbol))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if selection.get("mode") == "threshold":
        bar = float(selection.get("threshold") or 0)
        chosen = [symbol for score, symbol in ranked if score >= bar]
    else:
        chosen = [symbol for _score_value, symbol in ranked[: int(selection.get("top_n") or 1)]]
    if not chosen:
        return {}
    weight = min(float(spec["target_weight"]), 0.40)
    raw = [weight] * len(chosen)
    cap = max(0.0, 1.0 - float(spec["cash_floor"]))
    if sum(raw) > cap and sum(raw) > 0:
        scale = cap / sum(raw)
        raw = [item * scale for item in raw]
    return dict(zip(chosen, raw))


class RuleRunner:
    """Trade one spec. Drawdown is measured on this account only."""

    def __init__(self, spec: dict, features: Features):
        self.spec = spec
        self.features = features
        self.peak = 0.0
        self.name = f"rule:{spec.get('spec_id') or spec.get('name')}"

    def decide(self, request: dict, feed: Any) -> dict:
        account = request["account"]
        equity = float(account.get("equity") or 0)
        self.peak = max(self.peak, equity)
        drawdown = (self.peak - equity) / self.peak if self.peak else 0.0
        positions = {row["symbol"]: int(row["quantity"]) for row in account.get("positions") or [] if row.get("quantity")}
        if float(self.spec.get("drawdown_cut") or 0) > 0 and drawdown >= float(self.spec["drawdown_cut"]) and positions:
            orders = [
                make_order(request, f"cut_{request['step']:05d}_{index:02d}", symbol, "SELL", qty)
                for index, (symbol, qty) in enumerate(sorted(positions.items()))
            ]
            return envelope(request, orders, "rule: drawdown cut, sell to cash")
        step = int(request["step"])
        every = int(self.spec.get("rebalance_every") or 1)
        if step != 0 and step % every != 0:
            return envelope(request, [], "rule: between rebalances")
        rows = self.features(str(request.get("as_of") or ""))
        tradable = list(request["data"]["symbols"])
        targets = _targets(self.spec, rows, tradable)
        marks = feed.marks()
        slip = 1 + float(request["limits"]["slippage_bps"]) / 10_000
        minimum = float(self.spec.get("min_weight_change") or 0)
        cash = float(account.get("cash") or 0)
        planned: list[tuple[str, str, int, float]] = []
        for symbol in sorted(set(positions) | set(targets)):
            mark = (marks.get(symbol) or {}).get("close")
            if not mark or equity <= 0:
                continue
            held = positions.get(symbol, 0)
            current = held * float(mark) / equity
            target = targets.get(symbol, 0.0)
            if abs(target - current) < minimum:
                continue
            target_qty = math.floor(target * equity / (float(mark) * slip)) if target else 0
            delta = target_qty - held
            if delta < 0:
                planned.append((symbol, "SELL", -delta, float(mark)))
            elif delta > 0:
                planned.append((symbol, "BUY", delta, float(mark)))
        orders = []
        for index, (symbol, side, qty, _mark) in enumerate(item for item in planned if item[1] == "SELL"):
            orders.append(make_order(request, f"r_{step:05d}_s{index:02d}", symbol, "SELL", qty))
            cash += qty * _mark * (2 - slip)
        for index, (symbol, side, qty, mark) in enumerate(item for item in planned if item[1] == "BUY"):
            unit = mark * slip
            affordable = math.floor(cash / unit) if unit else 0
            qty = min(qty, affordable)
            if qty <= 0:
                continue
            orders.append(make_order(request, f"r_{step:05d}_b{index:02d}", symbol, "BUY", qty))
            cash -= qty * unit
        if not orders:
            return envelope(request, [], "rule: weights already match")
        return envelope(request, orders, f"rule: {self.spec.get('name')}")
