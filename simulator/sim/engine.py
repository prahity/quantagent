"""Run clock, data release, order processing and the official report for one run."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from contracts.action import Acknowledgement, ActionResponse, OrderOutcome, ValidationError, validate_action
from contracts.decision_request import Account, Clock, DataRefs, DecisionRequest, Limits, Position
from contracts.report import EquityPoint, Fill, Rejection, Report
from sim.broker import ExecutionConfig, Ledger
from sim.config import load_environment, universe_symbols
from sim.feed import Feed
from sim.metrics import max_drawdown_pct, sharpe, trade_totals, with_drawdown
from sim.news_queue import NewsQueue
from sim.scheduler import PendingOrder, Scheduler
from sim.store import NOMINAL_SECONDS, MarketTimeline, SnapshotStore


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default_factory=lambda: f"run_{uuid.uuid4().hex[:8]}")
    label: str | None = None
    interval: Literal["hourly", "daily"] = "hourly"
    mode: Literal["V1", "V2"] = "V1"
    initial_cash: float = Field(default=10_000, gt=0)
    # Omitted configs stay at 0 so earlier runs reproduce. A funded trial sets 200 and 0.05.
    daily_deposit: float = Field(default=0, ge=0, description="Cash credited once at the start of each trading day, before the decision.")
    cash_annual_rate: float = Field(default=0, description="Fixed annual rate on cash. Daily credit is cash * rate / 252. Never taken from prices.")
    currency: str = "USD"
    start: str = Field(description="inclusive date, YYYY-MM-DD")
    end: str = Field(description="inclusive date, YYYY-MM-DD")
    symbols: list[str] = Field(default_factory=list)
    time_scale: float = Field(default=60, gt=0)
    execution: dict[str, Any] = Field(default_factory=dict)
    news: dict[str, Any] = Field(default_factory=dict)
    news_lookback_days: int = Field(default=90, ge=0, description="How far before start eligible news history is pre-released")
    history_lookback_days: int = Field(default=45, ge=0, description="Completed price history available before the first decision")
    environment_version: str = "sim-0.1.0"

    @classmethod
    def from_defaults(cls, overrides: dict[str, Any] | None = None) -> "RunConfig":
        env = load_environment()
        base = {
            "interval": env.get("interval", "hourly"),
            "mode": env.get("mode", "V1"),
            "initial_cash": env.get("initial_cash", 10_000),
            "daily_deposit": env.get("daily_deposit", 0),
            "cash_annual_rate": env.get("cash_annual_rate", 0),
            "currency": env.get("currency", "USD"),
            "time_scale": (env.get("clock") or {}).get("time_scale", 60),
            "execution": env.get("execution") or {},
            "news": env.get("news") or {},
            "environment_version": env.get("environment_version", "sim-0.1.0"),
            "symbols": universe_symbols(),
        }
        merged = dict(base)
        for key, value in (overrides or {}).items():
            if value is None:
                continue
            if key in ("execution", "news") and isinstance(value, dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return cls(**merged)


class StepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: int
    request_id: str
    as_of: datetime
    response: dict | None
    accepted: bool
    errors: list[ValidationError]
    order_outcomes: list[OrderOutcome]
    released_news: list[str]


class Engine:
    def __init__(self, config: RunConfig, store: SnapshotStore):
        self.config = config
        self.store = store
        self.execution = ExecutionConfig.from_dict(config.execution)
        env = load_environment()
        self.news_cfg = {**(env.get("news") or {}), **config.news}
        self.sharpe_ppy = int(((env.get("report") or {}).get("sharpe_periods_per_year") or {}).get(config.interval, 252))

        unknown = sorted(set(config.symbols) - set(store.symbols(config.interval)))
        if unknown:
            raise ValueError(f"symbols not in the {config.interval} snapshot: {unknown}")
        start = pd.Timestamp(config.start, tz="UTC")
        end_exclusive = pd.Timestamp(config.end, tz="UTC") + pd.Timedelta(days=1)
        if end_exclusive <= start:
            raise ValueError("end must be on or after start")
        history_start = start - pd.Timedelta(days=config.history_lookback_days)
        bars = store.bars(config.interval, config.symbols, history_start, end_exclusive)
        self.market = MarketTimeline(bars, config.interval, decision_start=start)
        self.symbols = sorted(s for s in self.market.series if self.market.has_decision_bars(s))
        if not self.symbols:
            raise ValueError("no symbol has bars inside the run window")
        lookback = start - pd.Timedelta(days=config.news_lookback_days)
        self.news = NewsQueue(store.news(self.symbols, end_exclusive, lookback))
        self.feed = Feed(self.market, self.news, self.symbols)
        self.ledger = Ledger(config.initial_cash, self.execution)
        self.scheduler = Scheduler(self.execution.order_validity_bars)

        self.step = -1
        self.status: Literal["created", "awaiting_action", "finished"] = "created"
        self.current_request: DecisionRequest | None = None
        self.records: list[StepRecord] = []
        self.equity_points: list[tuple[datetime, float, float]] = []
        self.invalid_responses = 0
        self.held_requests = 0
        self.last_released: list[str] = []
        self._report: Report | None = None
        self.deposit_today = 0.0
        self.daily_cash: list[dict] = []
        self._opened_days: set[date] = set()
        self._closed_days: set[date] = set()
        self._segment_day: date | None = None
        self._segment_deposit = 0.0
        self._segment_decision_cash = 0.0
        self._segment_start_equity: float | None = None
        self._twr_factors: list[float] = []
        self._deposits_at_point: list[float] = []

    # ------------------------------------------------------------------ lifecycle
    def reset(self) -> DecisionRequest:
        self.step = 0
        self.status = "awaiting_action"
        t0 = self.market.boundary(0)
        self.last_released = self.news.release_through(t0)
        self.feed.set_clock(t0)
        self._open_day(t0)
        self._record_equity(t0, self.ledger.cash, self.ledger.cash)
        self.current_request = self._build_request()
        return self.current_request

    def submit_action(self, payload: Any) -> tuple[Acknowledgement, DecisionRequest | None, Report | None]:
        if self.status != "awaiting_action" or self.current_request is None:
            raise RuntimeError(f"run is {self.status}; no decision request is open")
        request = self.current_request
        response, errors = validate_action(payload, request, set(self.symbols), self.execution.max_orders_per_request)

        outcomes: list[OrderOutcome] = []
        if response is None:
            self.invalid_responses += 1
            ack = Acknowledgement(run_id=request.run_id, request_id=request.request_id, accepted=False, errors=errors)
        else:
            if not response.orders:
                self.held_requests += 1
            for order in response.orders:
                self.scheduler.add(order, request.request_id, self.step)
                outcomes.append(OrderOutcome(order_id=order.order_id, status="accepted"))
            ack = Acknowledgement(run_id=request.run_id, request_id=request.request_id, accepted=True, orders=outcomes)

        record = StepRecord(
            step=self.step,
            request_id=request.request_id,
            as_of=request.as_of,
            response=payload if isinstance(payload, dict) else {"_raw": str(payload)[:2000]},
            accepted=response is not None,
            errors=errors,
            order_outcomes=outcomes,
            released_news=list(self.last_released),
        )
        self.records.append(record)

        # V1 orders (execute_at == boundary) fill at this bar's open right away.
        self._process_due(self.market.boundary(self.step))

        if self.step + 1 >= self.market.steps:
            return ack, None, self._finish()

        current_day = self._trading_day(self.market.boundary(self.step))
        self.step += 1
        t = self.market.boundary(self.step)
        next_day = self._trading_day(t)
        if next_day != current_day:
            # Yesterday's interest uses cash after that day's fills and before today's deposit.
            self._close_day(t, final=False)
        self._process_due(t)
        self._expire(t)
        self.last_released = self.news.release_through(t)
        self.feed.set_clock(t)
        if next_day != current_day:
            self._open_day(t)
        else:
            self.deposit_today = 0.0
        marks = self._marks(t)
        self._record_equity(t, self.ledger.cash, self.ledger.equity(marks))
        self.current_request = self._build_request()
        return ack, self.current_request, None

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _trading_day(ts) -> date:
        """UTC date of a bar start. US session opens share one UTC date per trading day."""
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        else:
            t = t.tz_convert("UTC")
        return t.date()

    def _record_equity(self, ts, cash: float, equity: float) -> None:
        when = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        self.equity_points.append((when, cash, equity))
        self._deposits_at_point.append(self.ledger.total_deposits)

    def _open_day(self, t: pd.Timestamp) -> None:
        """Credit today's deposit once, before the decision, and start the return segment."""
        day = self._trading_day(t)
        if day in self._opened_days:
            return
        self._opened_days.add(day)
        amount = float(self.config.daily_deposit or 0)
        self.deposit_today = 0.0
        if amount > 0:
            self.ledger.credit("deposit", amount, t, day.isoformat())
            self.deposit_today = amount
        self._segment_day = day
        self._segment_deposit = self.deposit_today
        self._segment_decision_cash = self.ledger.cash
        self._segment_start_equity = self.ledger.equity(self._marks(t))

    def _close_day(self, mark_time: pd.Timestamp, final: bool) -> None:
        """Credit one day of cash interest after that day's fills and before the next deposit."""
        day = self._segment_day
        if day is None or day in self._closed_days:
            return
        uninvested = self.ledger.cash
        rate = float(self.config.cash_annual_rate or 0)
        interest = 0.0
        if rate:
            interest = uninvested * rate / 252.0
            self.ledger.credit("interest", interest, self.market.boundary(self.step), day.isoformat())
        if final:
            marks = {s: v for s in self.symbols if (v := self.market.last_bar_close_in_run(s)) is not None}
        else:
            marks = self._marks(mark_time)
        end_equity = self.ledger.equity(marks)
        start = self._segment_start_equity or 0.0
        if start > 0:
            self._twr_factors.append(end_equity / start)
        self.daily_cash.append(
            {
                "trading_day": day.isoformat(),
                "cash_at_decision": self._segment_decision_cash,
                "cash_uninvested": uninvested,
                "deposit": self._segment_deposit,
                "interest": interest,
            }
        )
        self._closed_days.add(day)

    def _marks(self, t: pd.Timestamp) -> dict[str, float]:
        marks = {}
        for symbol in self.symbols:
            last = self.market.last_close_through(symbol, t)
            if last is not None:
                marks[symbol] = last[0]
        return marks

    def _process_due(self, t: pd.Timestamp) -> None:
        marks = self._marks(t)
        for pending in self.scheduler.due(t):
            bar_open = self.market.open_at(pending.order.symbol, t)
            if bar_open is None:
                continue  # no market event for this symbol at t; wait or expire later
            result = self.ledger.execute(pending.order, pending.request_id, bar_open, t, marks)
            if isinstance(result, Rejection) and result.reason == "limit_not_marketable":
                continue  # keeps waiting for a later bar until it expires
            self.scheduler.remove(pending)
            self._update_outcome(pending, result)

    def _expire(self, t: pd.Timestamp) -> None:
        for pending in self.scheduler.expired(self.step):
            self.scheduler.remove(pending)
            rejection = self.ledger.reject(pending.order, pending.request_id, t, "expired")
            self._update_outcome(pending, rejection)

    def _update_outcome(self, pending: PendingOrder, result: Fill | Rejection) -> None:
        for record in self.records:
            if record.request_id != pending.request_id:
                continue
            for outcome in record.order_outcomes:
                if outcome.order_id == pending.order.order_id:
                    if isinstance(result, Rejection):
                        outcome.status = "rejected"
                        outcome.reason = result.reason
                    else:
                        outcome.reason = f"filled@{result.fill_price:.4f}"

    def _build_request(self) -> DecisionRequest:
        t = self.market.boundary(self.step)
        marks = self._marks(t)
        positions = [Position(**row) for row in self.ledger.position_rows(marks)]
        digest = self.news.digest(
            t,
            self.ledger.held_symbols(),
            self.symbols,
            int(self.news_cfg.get("digest_max_events", 20)),
            bool(self.news_cfg.get("prioritize_positions", True)),
        )
        return DecisionRequest(
            run_id=self.config.run_id,
            request_id=f"decision_{self.step:05d}",
            mode=self.config.mode,
            as_of=t.to_pydatetime(),
            window_start=t.to_pydatetime(),
            window_end=self.market.window_end(self.step).to_pydatetime(),
            initial_cash=self.config.initial_cash,
            account=Account(
                currency=self.config.currency,
                cash=self.ledger.cash,
                equity=self.ledger.equity(marks),
                positions=positions,
                deposit_today=self.deposit_today,
                total_deposits=self.ledger.total_deposits,
                interest_earned=self.ledger.interest_earned,
                cash_annual_rate=float(self.config.cash_annual_rate),
            ),
            clock=Clock(
                decision_interval_seconds=NOMINAL_SECONDS[self.config.interval],
                time_scale=self.config.time_scale,
                execution_resolution_ms=NOMINAL_SECONDS[self.config.interval] * 1000,
                interval=self.config.interval,
            ),
            data=DataRefs(
                feed_schema=f"feed_{self.config.run_id}",
                workspace_schema=f"agent_{self.config.run_id}",
                news_event_ids=digest,
                symbols=self.symbols,
            ),
            limits=Limits(
                max_orders_per_request=self.execution.max_orders_per_request,
                max_position_pct_equity=self.execution.max_position_pct_equity,
                allow_shorting=self.execution.allow_shorting,
                order_validity_bars=self.execution.order_validity_bars,
                slippage_bps=self.execution.slippage_bps,
                commission_per_share=self.execution.commission_per_share,
            ),
            step=self.step,
            steps_total=self.market.steps,
        )

    def _finish(self) -> Report:
        t_end = self.market.window_end(self.step)
        for pending in self.scheduler.drain():
            rejection = self.ledger.reject(pending.order, pending.request_id, t_end, "run_ended")
            self._update_outcome(pending, rejection)
        self._close_day(t_end, final=True)
        marks = {s: v for s in self.symbols if (v := self.market.last_bar_close_in_run(s)) is not None}
        self._final_marks = marks
        final_equity = self.ledger.equity(marks)
        self._record_equity(t_end, self.ledger.cash, final_equity)
        self.status = "finished"
        self.current_request = None
        self.feed.set_clock(t_end)

        equity = [e for _, _, e in self.equity_points]
        # A deposit raises equity without being a gain. Subtract deposits already credited at each point.
        if self.ledger.total_deposits == 0:
            drawdown_series = equity
        else:
            drawdown_series = [value - deposited for value, deposited in zip(equity, self._deposits_at_point)]
        totals = trade_totals(self.ledger.fills)
        simple_return = (final_equity / self.config.initial_cash - 1) * 100
        if self.ledger.total_deposits == 0:
            twr = simple_return
        else:
            factor = 1.0
            for piece in self._twr_factors:
                factor *= piece
            twr = (factor - 1.0) * 100.0
        self._report = Report(
            run_id=self.config.run_id,
            label=self.config.label,
            environment_version=self.config.environment_version,
            data_version=self.store.data_version,
            interval=self.config.interval,
            mode=self.config.mode,
            symbols=self.symbols,
            start=self.market.boundary(0).to_pydatetime(),
            end=t_end.to_pydatetime(),
            steps=self.market.steps,
            initial_cash=self.config.initial_cash,
            final_cash=self.ledger.cash,
            final_equity=final_equity,
            net_profit=final_equity - self.config.initial_cash - self.ledger.total_deposits,
            return_pct=simple_return,
            max_drawdown_pct=max_drawdown_pct(drawdown_series),
            total_deposits=self.ledger.total_deposits,
            interest_earned=self.ledger.interest_earned,
            time_weighted_return_pct=twr,
            daily_deposit=float(self.config.daily_deposit),
            cash_annual_rate=float(self.config.cash_annual_rate),
            daily_cash=self.daily_cash,
            sharpe=sharpe(equity, self.sharpe_ppy),
            sharpe_periods_per_year=self.sharpe_ppy,
            turnover=totals["traded_notional"] / self.config.initial_cash,
            traded_notional=totals["traded_notional"],
            commissions=totals["commissions"],
            slippage_cost=totals["slippage_cost"],
            fills=len(self.ledger.fills),
            rejections=len(self.ledger.rejections),
            invalid_responses=self.invalid_responses,
            held_requests=self.held_requests,
            final_positions=self.ledger.position_rows(marks),
            valuation_convention="open positions marked at the close of each symbol's last bar inside the run window",
            costs=self.execution.as_dict(),
        )
        return self._report

    # ------------------------------------------------------------------ read side
    @property
    def report(self) -> Report | None:
        return self._report

    def equity_curve(self) -> list[EquityPoint]:
        return with_drawdown(self.equity_points)

    def state(self) -> dict:
        t = self.feed.clock
        if self.status == "finished":
            marks = getattr(self, "_final_marks", {})
        else:
            marks = self._marks(t) if t is not None else {}
        return {
            "run_id": self.config.run_id,
            "label": self.config.label,
            "status": self.status,
            "step": self.step,
            "steps_total": self.market.steps,
            "as_of": None if t is None else t.isoformat(),
            "config": self.config.model_dump(),
            "data_version": self.store.data_version,
            "symbols": self.symbols,
            "cash": self.ledger.cash,
            "equity": self.equity_points[-1][2] if self.equity_points else self.config.initial_cash,
            "positions": self.ledger.position_rows(marks),
            "marks": marks,
            "pending_orders": self.scheduler.rows(),
            "fills": len(self.ledger.fills),
            "rejections": len(self.ledger.rejections),
            "invalid_responses": self.invalid_responses,
            "held_requests": self.held_requests,
            "news_total_in_window": self.news.total(),
            "news_released": self.news.released_count(),
            "current_request": None if self.current_request is None else self.current_request.model_dump(mode="json"),
        }
