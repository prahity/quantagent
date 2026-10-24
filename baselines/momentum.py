"""A scripted momentum agent: no LLM, same query tool and JSON contract as the policy.

Each decision it reads recent closes for every eligible symbol through the cutoff-bound
query tool, ranks them by trailing return, and rotates into the single strongest symbol
when its momentum is positive (selling anything else it holds). It respects the published
position cap and available cash, and validates its envelope before returning. Deterministic:
the same feed and config give the same orders every time.

Imports no simulator and no model code.
"""
from __future__ import annotations

import math

from agent import contract
from agent.memory import Memory
from agent.query_tool import QueryTool


class MomentumAgent:
    name = "momentum"

    def __init__(self, lookback: int = 5, threshold: float = 0.0):
        self.lookback = lookback
        self.threshold = threshold

    # Uniform agent interface; the scripted baseline needs no run-start step.
    def start_run(self, run_id: str, request: dict) -> None:
        return None

    def _recent_closes(self, query_tool: QueryTool, symbol: str) -> list[float]:
        result = query_tool(
            goal=f"trailing closes for {symbol} momentum",
            sql="SELECT ts, close FROM prices WHERE symbol = :sym ORDER BY ts DESC LIMIT :n",
            params={"sym": symbol, "n": self.lookback + 1},
        )
        if not result.ok:
            return []
        # rows come newest-first; return closes oldest->newest
        closes = [float(r["close"]) for r in result.rows if r.get("close") is not None]
        return list(reversed(closes))

    def decide(self, request: dict, query_tool: QueryTool, memory: Memory) -> dict:
        symbols = request["data"]["symbols"]
        equity = float(request["account"]["equity"])
        cash = float(request["account"]["cash"])
        cap = float(request["limits"]["max_position_pct_equity"]) or 1.0
        slip = 1 + float(request["limits"]["slippage_bps"]) / 10_000.0

        positions = {p["symbol"]: int(p["quantity"]) for p in request["account"]["positions"]}
        momentum: dict[str, float] = {}
        last_close: dict[str, float] = {}
        for symbol in symbols:
            closes = self._recent_closes(query_tool, symbol)
            if len(closes) < self.lookback + 1 or closes[0] <= 0:
                continue
            momentum[symbol] = closes[-1] / closes[0] - 1.0
            last_close[symbol] = closes[-1]

        best = max(momentum, key=momentum.get) if momentum else None
        if best is not None and momentum[best] <= self.threshold:
            best = None

        orders: list[dict] = []
        # Exit anything that is not the chosen symbol (or exit everything when none chosen).
        for symbol, qty in positions.items():
            if qty > 0 and symbol != best:
                orders.append(contract.make_order(request, f"sell_{symbol}", symbol, "SELL", qty))

        # Enter / top up the chosen symbol up to the position cap.
        if best is not None and best in last_close:
            price = last_close[best] * slip
            target_value = cap * equity * 0.98
            target_qty = int(math.floor(target_value / price)) if price > 0 else 0
            held = positions.get(best, 0)
            buy_qty = target_qty - held
            # Do not exceed cash available this step (sells settle at the same bar open).
            affordable = int(math.floor(cash / price)) if price > 0 else 0
            buy_qty = min(buy_qty, affordable)
            if buy_qty > 0:
                orders.append(contract.make_order(request, f"buy_{best}", best, "BUY", buy_qty))

        reason = (
            f"momentum pick {best} ({momentum.get(best, 0.0):+.3%})" if best else "no positive momentum; hold/exit"
        )
        envelope = contract.build_envelope(request, orders, reason)
        errors = contract.validate_envelope(
            envelope, request, set(symbols), request["limits"]["max_orders_per_request"]
        )
        if errors:
            return contract.hold(request, "momentum envelope invalid; holding")
        return envelope
