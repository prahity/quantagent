"""One trading decision through Meterless. Nightshift selects the Nemotron lane.

The model sees only the decision request the simulator already released. It
answers with orders and a reason. ``structure_action`` copies the request ids
and window onto that reply, and ``fit_orders`` shrinks a buy that would exceed
cash or the position cap. A reply that is not JSON becomes a hold, which the
simulator records. The model does not choose the fill price.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from meterless.action import fit_orders, structure_action
from meterless.provider import MeterlessRuntime
from meterless.query import AnalysisSource, QueryRequest, QueryResult, column_hint
from meterless.recall import fetch_brief

_REPLY = """Reply with one JSON object and no other text:
{"orders":[{"symbol":"AAPL","side":"BUY","quantity":10,"order_type":"MARKET"}],"reason":"one or two sentences"}
orders may be empty, which means hold. side is BUY or SELL. quantity is the whole shares you choose, sized from the latest close and the cash you want in that symbol. There is no per-symbol size cap. order_type MARKET needs no limit price. A BUY must fit in cash. You can only SELL shares you hold.
Use only the symbols, prices, and filings given here. Do not assume anything after as_of.

Request:
"""

_ECONOMICS = (
    "You are the trading agent in a market simulator. Your goal is to maximize profit. "
    "Profit is final equity minus starting cash minus deposits. "
    "You can buy stocks or keep cash. Cash you leave uninvested earns account.cash_annual_rate, "
    "credited once each trading day as cash times that rate divided by 252. You cannot create cash. "
    "account.deposit_today is new cash the simulator added at the start of this trading day and it is already inside account.cash. "
    "It is non-zero only on the first decision of that day. account.total_deposits excludes starting cash. "
    "account.interest_earned is interest already credited. "
)

_PROMPT_STYLES = {
    "default": (
        _ECONOMICS
        + "If you hold no shares and recent closes are higher than earlier ones, you may buy. Keeping cash to earn interest is allowed.\n"
        + _REPLY
    ),
    "deploy": (
        _ECONOMICS
        + "Idle cash earns only the cash rate, which lags buy-and-hold when stocks are rising. If cash is more than about 20% of equity and any "
        "symbol's recent closes are rising, buy enough shares to put most of that cash to work. Split across rising "
        "symbols when more than one is rising. Do not wait for a perfect signal.\n"
        + _REPLY
    ),
    "equal": (
        _ECONOMICS
        + "When you are fully in cash, or nearly so, buy roughly equal dollar amounts of every tradeable symbol that "
        "has a recent close, using nearly all of the cash. After that, hold unless a clear drop forces a sell. "
        "You may instead keep cash if earning interest is the better way to maximize profit.\n"
        + _REPLY
    ),
}

_PROMPT = _PROMPT_STYLES["default"]

_BARS = 10
_QUERY_LIMIT = 1
_SQL_REPAIRS = 1  # one more query, only after an SQL error
_RULE_FEATURES = ("close", "ret_5", "ret_21", "ret_63", "vol_21", "ma_gap_21", "ma_gap_63", "dd_63", "rs_spy")
_MAX_TOKENS = 700
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_SIM_RUNS = Path(__file__).resolve().parents[1] / "simulator" / "runs"

_QUERY_PROMPT = """You are the trading agent. Historical prices, filings, and the feature view are in the SQL relations below, already cut off at this decision's as_of. Use SQL when you need history. Prefer `features` for returns, volatility, moving-average gaps, drawdown, relative strength versus SPY, and filing distances. Do not estimate a return, a price, or a filing date.
{schema}
These are the only columns. Any other name fails:
{columns}
`features` has one row per symbol at as_of. It has no ts, interval, or as_of column, so do not filter or sort it by time. Use `bars` for a price series.
Examples that run:
SELECT symbol, close, ret_5, ret_21, ret_63, vol_21, ma_gap_21, ma_gap_63, dd_63, rs_spy FROM features ORDER BY symbol
SELECT symbol, ts, close FROM bars WHERE interval = 'daily' AND symbol = 'AAPL' ORDER BY ts DESC LIMIT 10
SELECT symbol, form, title, published_at FROM filings ORDER BY published_at DESC LIMIT 5
Return either {{"goal":"<what you need to know>","sql":"<one SELECT>"}} or the action JSON from the trading instructions.
The simulator's as_of is fixed. Putting a later time in the SQL does not reveal it. A feature column is null when the released history is shorter than that window.

Trading instructions:
"""


def _json_object(text: str) -> dict[str, Any] | None:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _closes(visible: dict) -> dict[str, float]:
    prices = {}
    for symbol, bars in (visible.get("recent_bars") or {}).items():
        if bars:
            prices[symbol] = float(bars[-1]["close"])
    return prices


def _ready(parsed: dict, request: dict, visible: dict) -> tuple[dict, list[dict]]:
    """Return the repaired action and the orders the model actually asked for."""
    structured = structure_action(parsed, request)
    original = [dict(order) for order in structured["orders"]]
    return fit_orders(structured, request, _closes(visible)), original


def _hold(request: dict, reason: str) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": request["run_id"],
        "request_id": request["request_id"],
        "mode": request["mode"],
        "window_start": request["window_start"],
        "window_end": request["window_end"],
        "orders": [],
        "reason": reason[:2000],
    }


def _market(request: dict, feed: Any) -> dict:
    """Released bars and filings from the simulator feed. The feed refuses anything after as_of."""
    symbols = (request.get("data") or {}).get("symbols") or []
    bars = {}
    for symbol in symbols:
        bars[symbol] = [
            {
                "start": str(b["ts"])[:16],
                **{k: round(float(b[k]), 2) for k in ("open", "high", "low", "close")},
                "volume": int(b["volume"]),
            }
            for b in feed.prices(symbol, _BARS)
        ]
    wanted = set((request.get("data") or {}).get("news_event_ids") or [])
    filings = [
        {"symbol": n["symbol"], "published_at": str(n["published_at"])[:16], "title": n["title"]}
        for n in feed.news(symbols) if n["event_id"] in wanted
    ]
    return {"recent_bars": bars, "released_filings": filings}


_MEMORY_HEADER = """
Earlier orders in this run, oldest first, with the simulator's acknowledgement. Use them to avoid repeating rejected orders. Decide each step afresh from the current data:
"""

_NOTES_HEADER = """
Strategy notebook. You wrote it after reviewing your results in earlier, completed periods. Apply its rules where they fit the current data:
"""

_RULES_HEADER = """
Notebook rules run on your account and today's released features. A target is a fraction of equity; the rest is cash. The orders are what the rule itself places today, sized from your cash and holdings; an empty list means the rule holds. These figures are computed, not missing.
"""

_RULES_TASK = """Choose one:
(a) Apply one listed rule. Set "rule" to its exact name and copy that rule's orders for today exactly, including an empty list on a day it holds. The first rule is the notebook's recommendation; use it unless today's figures argue against it.
(b) Apply none. Set "rule" to "none" and set "why_not" to figures shown here (today's features, the notebook's results) that make every listed rule wrong today. Low confidence alone is not a reason: the notebook weighed it when it chose the recommendation, and cash has no out-of-sample record either.
Keeping more than half of equity in cash is allowed only if the chosen rule's targets say so, or under (b) with that evidence stated in why_not.
Add "rule" and "why_not" to the action JSON: {"rule":"<name or none>","why_not":"<empty when a rule is applied>","orders":[...],"reason":"..."}
"""


class NemotronTrader:
    """Calls Meterless Runtime for each simulator decision request.

    ``memory`` is the number of earlier orders from this run included in the
    prompt. ``notes`` is a strategy notebook carried in from earlier runs. With
    both off, each decision sees only the current request and feed.
    """

    name = "nemotron"

    def __init__(
        self,
        runtime: MeterlessRuntime,
        memory: int = 0,
        notes: str = "",
        retries: int = 0,
        source: AnalysisSource | None = None,
        recall: str = "sql",
        prompt_style: str = "default",
        log_dir: str | Path | None = None,
        rule_view: Any = None,
    ):
        self.runtime = runtime
        self.rule_view = rule_view
        self.rule_choices: list[dict] = []
        self.memory = memory
        self.notes = notes
        self.retries = retries
        self.source = source
        self.recall = recall
        self.prompt_style = prompt_style if prompt_style in _PROMPT_STYLES else "default"
        self.log_dir = Path(log_dir) if log_dir else None
        self.log: list[dict] = []
        self.queries: list[QueryResult] = []
        self.repairs: list[dict] = []

    def _trade_prompt(self) -> str:
        return _PROMPT_STYLES[self.prompt_style]

    def observe(self, request: dict, payload: dict, ack: dict) -> None:
        if self.memory <= 0:
            return
        self.log.append({
            "step": request.get("step"),
            "as_of": request.get("as_of"),
            "orders": [
                {k: o.get(k) for k in ("symbol", "side", "quantity", "order_type", "limit_price")}
                for o in payload.get("orders") or [] if isinstance(o, dict)
            ],
            "accepted": ack.get("accepted"),
            "order_status": [{"status": o.get("status"), "reason": o.get("reason")} for o in ack.get("orders", [])],
            "errors": [e.get("message") for e in ack.get("errors", [])],
        })
        del self.log[: -self.memory]

    def _prompt(self, visible: dict, rules: list[dict] | None = None) -> str:
        prompt = self._trade_prompt() + json.dumps(visible)
        if self.notes:
            prompt += "\n" + _NOTES_HEADER + self.notes
        if rules:
            prompt += _rules_text(rules, visible)
        if self.memory > 0 and self.log:
            prompt += "\n" + _MEMORY_HEADER + "\n".join(json.dumps(entry) for entry in self.log)
        return prompt

    def _visible(self, request: dict, feed: Any) -> dict:
        visible = {
            "schema_version": request.get("schema_version"),
            "run_id": request.get("run_id"),
            "request_id": request.get("request_id"),
            "mode": request.get("mode"),
            "as_of": request.get("as_of"),
            "window_start": request.get("window_start"),
            "window_end": request.get("window_end"),
            "account": request.get("account"),
            "data": request.get("data"),
            "limits": request.get("limits"),
            "step": request.get("step"),
            "steps_total": request.get("steps_total"),
        }
        if feed is not None:
            visible.update(_market(request, feed))
        return visible

    def _log_root(self, request: dict) -> Path:
        if self.log_dir is not None:
            return self.log_dir
        return _SIM_RUNS / str(request.get("run_id") or "unknown")

    def _append_log(self, request: dict, name: str, record: dict) -> None:
        path = self._log_root(request) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def _log_query(self, request: dict, result: QueryResult, model_sql: str | None = None) -> None:
        self.queries.append(result)
        payload = {"request_id": request.get("request_id"), "as_of": request.get("as_of"), **result.as_dict()}
        if model_sql:
            payload["sql"] = model_sql
        self._append_log(request, "queries.jsonl", payload)

    def _log_model_call(self, request: dict, attempt: int, error: str | None) -> None:
        recorded = getattr(self.runtime, "last_completion", None)
        self._append_log(request, "model_calls.jsonl", {
            "request_id": request.get("request_id"),
            "as_of": request.get("as_of"),
            "attempt": attempt,
            "status": getattr(recorded, "status", None),
            "latency_ms": getattr(recorded, "latency_ms", None),
            "finish_reason": getattr(recorded, "finish_reason", None),
            "prompt_tokens": getattr(recorded, "prompt_tokens", None),
            "completion_tokens": getattr(recorded, "completion_tokens", None),
            "total_tokens": getattr(recorded, "total_tokens", None),
            "reported_model": getattr(recorded, "reported_model", None),
            "endpoint_host": getattr(recorded, "endpoint_host", None),
            "route": getattr(recorded, "route", None) or {},
            "routing_headers": getattr(recorded, "routing_headers", None) or {},
            "reasoning": bool(getattr(recorded, "reasoning", False)),
            "error": error or getattr(recorded, "error", None),
        })

    def _complete(self, prompt: str, request: dict) -> str | dict:
        for attempt in range(self.retries + 1):
            try:
                text = self.runtime.complete(prompt, max_tokens=_MAX_TOKENS)
            except Exception as exc:
                self._log_model_call(request, attempt, type(exc).__name__)
                if attempt == self.retries:
                    return _hold(request, f"meterless unavailable: {exc}")
                time.sleep(15 * (attempt + 1))
                continue
            self._log_model_call(request, attempt, None)
            return text
        return _hold(request, "meterless unavailable")

    def _query_prompt(self, visible: dict, found: list[QueryResult], rules: list[dict] | None = None) -> str:
        prompt = _QUERY_PROMPT.format(schema=self.source.schema_text, columns=column_hint()) + self._trade_prompt() + json.dumps(visible)
        if found:
            prompt += "\nSQL results for this decision. Cite these figures. Do not replace them:\n"
            prompt += "\n".join(_result_text(item) for item in found)
        if self.notes:
            prompt += "\n" + _NOTES_HEADER + self.notes
        if rules:
            prompt += _rules_text(rules, visible)
        if self.memory > 0 and self.log:
            prompt += "\n" + _MEMORY_HEADER + "\n".join(json.dumps(entry) for entry in self.log)
        return prompt

    def _ground_reason(self, reason: str, visible: dict, found: list[QueryResult]) -> tuple[str, bool]:
        """Keep the model's reason. Flag it when it cites a number that was not in the evidence.

        The flag is recorded beside the order repair. It is not added to the action:
        the simulator rejects any key outside the fixed envelope.
        """
        original = (reason or "")[:2000]
        sources = json.dumps(visible) + "\n" + "\n".join(item.text for item in found)
        unsupported = not set(_NUMBER.findall(original)) <= set(_NUMBER.findall(sources))
        return original, unsupported

    def _apply(self, parsed: dict, request: dict, visible: dict, found: list[QueryResult] | None,
               rules: list[dict] | None = None) -> dict:
        action, original_orders = _ready(parsed, request, visible)
        unsupported = None
        if found is not None:
            action["reason"], unsupported = self._ground_reason(action.get("reason") or "", visible, found)
        record = {
            "request_id": request.get("request_id"),
            "as_of": request.get("as_of"),
            "original_orders": original_orders,
            "repaired_orders": action.get("orders"),
            "reason": action.get("reason"),
            "reason_unsupported": unsupported,
        }
        if rules is not None:
            record["rule"] = self._log_rule(parsed, request, visible, rules, action)
        self.repairs.append(record)
        self._append_log(request, "order_repairs.jsonl", record)
        return action

    def _log_rule(self, parsed: dict, request: dict, visible: dict, rules: list[dict], action: dict) -> str:
        named = " ".join(str(parsed.get("rule") or "").split())[:80]
        by_name = {rule["name"]: rule for rule in rules}
        chosen = by_name.get(named)
        choice = {
            "request_id": request.get("request_id"),
            "as_of": request.get("as_of"),
            "step": request.get("step"),
            "rule": named or "(missing)",
            "listed": chosen is not None,
            "why_not": " ".join(str(parsed.get("why_not") or "").split())[:600],
            "rule_targets": None if chosen is None else chosen["targets"],
            "rule_orders": None if chosen is None else _order_keys(chosen.get("orders")),
            "matches_rule_orders": None if chosen is None else _order_keys(parsed.get("orders")) == _order_keys(chosen.get("orders")),
            "rebalance_today": None if chosen is None else chosen["rebalance_today"],
            "weights_before": _weights(visible),
            "orders": _order_keys(action.get("orders")),
            "recommended": rules[0]["name"] if rules else None,
            "recommended_orders": _order_keys(rules[0].get("orders")) if rules else None,
            "reason": action.get("reason"),
        }
        self.rule_choices.append(choice)
        self._append_log(request, "rule_choices.jsonl", choice)
        return choice["rule"]

    def _rules(self, request: dict, feed: Any) -> list[dict] | None:
        if self.rule_view is None:
            return None
        try:
            return self.rule_view(request, feed)
        except Exception as exc:
            self._append_log(request, "rule_choices.jsonl", {"request_id": request.get("request_id"), "error": f"rule view failed: {exc}"})
            return []

    def _execute(self, request: dict, parsed: dict, sql: str) -> QueryResult:
        result = self.source.execute(QueryRequest(
            run_id=str(request.get("run_id") or ""),
            request_id=str(request.get("request_id") or ""),
            query_id=f"{request.get('request_id')}-q{len(self.queries) + 1}",
            goal=str(parsed.get("goal") or "")[:300],
            sql=sql,
            as_of=str(request.get("as_of") or ""),
        ))
        self._log_query(request, result, sql.strip())
        return result

    def decide(self, request: dict, feed: Any = None) -> dict:
        visible = self._visible(request, feed)
        rules = self._rules(request, feed)
        if self.recall == "brief" and self.source is not None:
            brief, found = fetch_brief(self.source, request)
            for item in found:
                self._log_query(request, item)
            text = self._complete(self._prompt(visible, rules) + "\n" + brief, request)
            if isinstance(text, dict):
                return text
            parsed = _json_object(text)
            if parsed is None or "orders" not in parsed:
                return _hold(request, "model reply was not the action JSON")
            return self._apply(parsed, request, visible, found, rules)
        if self.source is None or self.recall == "notes":
            text = self._complete(self._prompt(visible, rules), request)
            if isinstance(text, dict):
                return text
            parsed = _json_object(text)
            if parsed is None:
                return _hold(request, "model reply was not the action JSON")
            return self._apply(parsed, request, visible, None, rules)

        found: list[QueryResult] = []
        planned, repairs = _QUERY_LIMIT, _SQL_REPAIRS
        while planned > 0:
            text = self._complete(self._query_prompt(visible, found, rules), request)
            if isinstance(text, dict):
                return text
            parsed = _json_object(text) or {}
            sql = parsed.get("sql")
            if isinstance(sql, str) and sql.strip():
                result = self._execute(request, parsed, sql)
                found.append(result)
                if result.error and repairs > 0:
                    repairs -= 1
                    continue
                planned -= 1
                continue
            if "orders" in parsed:
                return self._apply(parsed, request, visible, found, rules)
            break
        text = self._complete(self._query_prompt(visible, found, rules) + "\nReply with the action JSON only.", request)
        if isinstance(text, dict):
            return text
        parsed = _json_object(text)
        if parsed is None or "orders" not in parsed:
            return _hold(request, "model reply was not the action JSON")
        return self._apply(parsed, request, visible, found, rules)


def _result_text(item: QueryResult) -> str:
    if not item.error:
        return f"{item.query_id} {item.goal}\n{item.text}"
    return (
        f"{item.query_id} {item.goal}\nSQL error: {item.error}\n"
        f"Available columns:\n{column_hint()}\n"
        "Fix the query using only these columns, or give the action."
    )


def _weights(visible: dict) -> dict[str, float]:
    account = visible.get("account") or {}
    equity = float(account.get("equity") or 0)
    if equity <= 0:
        return {}
    out = {}
    for position in account.get("positions") or []:
        value = position.get("market_value")
        if value is None:
            value = float(position.get("quantity") or 0) * float(position.get("mark_price") or 0)
        if value:
            out[str(position.get("symbol"))] = round(float(value) / equity, 4)
    out["cash"] = round(float(account.get("cash") or 0) / equity, 4)
    return out


def _order_keys(orders: Any) -> list[list]:
    keys = []
    for order in orders or []:
        if isinstance(order, dict):
            try:
                keys.append([str(order.get("side", "")).upper(), int(float(order.get("quantity") or 0)), str(order.get("symbol", "")).upper()])
            except (TypeError, ValueError):
                continue
    return sorted(keys)


def _rules_text(rules: list[dict], visible: dict) -> str:
    lines = [_RULES_HEADER.rstrip("\n")]
    for rule in rules:
        targets = rule.get("targets") or {}
        held = ", ".join(f"{symbol} {weight * 100:.1f}%" for symbol, weight in sorted(targets.items()))
        cash = max(0.0, 1 - sum(targets.values())) * 100
        today = "yes" if rule.get("rebalance_today") else "no, the rule holds today"
        orders = json.dumps(rule.get("orders") or [])
        lines.append(
            f"- {rule['name']}: targets {held or 'no stock'}, cash {cash:.1f}%. "
            f"Orders today: {orders} ({rule.get('why_orders') or 'computed'}). "
            f"Rebalances every {rule['rebalance_every']} sessions; rebalance today: {today}. "
            f"Sells everything if its own drawdown reaches {float(rule.get('drawdown_cut') or 0) * 100:.0f}%. "
            f"Explore excess {rule['excess_pp']:+.2f} pp vs buy-and-hold, confidence {rule['confidence']}, "
            f"{rule.get('oos_months', 0)} out-of-sample months."
        )
    features = rules[0].get("features") if rules else None
    if features:
        lines.append("Today's released features:")
        for row in features:
            cells = ", ".join(
                f"{key} {'null' if row.get(key) is None else round(float(row[key]), 4)}"
                for key in _RULE_FEATURES
            )
            lines.append(f"  {row.get('symbol')}: {cells}")
    weights = _weights(visible)
    lines.append("Your weights now: " + (", ".join(f"{key} {value * 100:.1f}%" for key, value in weights.items()) or "unknown"))
    return "\n" + "\n".join(lines) + "\n" + _RULES_TASK
