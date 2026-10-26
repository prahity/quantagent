# Simulator local context

Working spec for the simulator slice. Source of truth for the product design remains `README.md`, `AI-Trader-Architecture.md`, and `AI-Trader-Build-Guide.txt` at the repository root (pulled at `ec6a48a`). This file narrows those documents to the independent simulator. It is local context, not a change to the design. What was actually built, and the choices behind it, is `context/work.md`. The sections below the status block are the original working spec.

## Status: built

Implemented in this `simulator/` folder, separate from the agent design docs at the repository root. Checked against real data (see `simulator/README.md`):

- `sim/`, `contracts/`, `agent/`, `runner.py`, `server/` (FastAPI, port 8711), `web/` (Next.js, port 3177), `scripts/fetch_data.py`, `tests/` (29 checks on real-data fixtures, including the AST import-boundary test).
- Data: Yahoo Finance hourly + daily bars and SEC EDGAR filings for 8 symbols, 2024-01-01 → 2026-07-21, snapshot `snap-17f602c41277a742` (gitignored; manifest committed). Optional Alpha Vantage / Finnhub news when free keys are present.
- All seven "checks that define done" below are covered by tests or recorded runs: costs change the book as configured; two V2 orders fill in time order at the next bar open; news gating by `published_at`/`available_at`/`released_at`; malformed JSON, late V2 timestamps, unknown symbols and duplicate submissions rejected and recorded; insufficient-inventory sells rejected; replay of `actions.jsonl` reproduces `report.json`; `sim/` never imports `agent/` and the feed raises `FutureAccessError` (HTTP 403) for a future `as_of`.

Decisions taken while building, agreed with the project owner:

| Spec item | What was built | Why |
| --- | --- | --- |
| One request per simulated minute | One request per completed **hourly or daily** bar, toggled per run (`interval`). `decision_interval_seconds` on the request reports the real cadence. | Free, verifiable minute data for 2024–2026 does not exist; minute cadence over hourly bars would fabricate resolution. |
| V2 intraminute activation | V2 `execute_at` is any time inside the bar window; fills at the open of the first bar starting at or after it. | Honest about data resolution: a completed bar's H/L/C are not known at its open. |
| `MARKET` only | `MARKET` and `LIMIT` (limit waits until marketable, expires after `order_validity_bars`). | Cheap to add; exercises rejection paths. |
| News eligibility by `published_at`/`available_at`/`released_at` | SEC acceptance time corrected from the API's mislabelled timestamp to real UTC (`sec_acceptance_to_utc`); `available_at` is conservative (06:00 ET on the filing date when acceptance was after hours). `availability_basis` and `acceptance_api_raw` kept for audit. | The EDGAR submissions API labels Eastern local time as `Z`; verified against filing index pages. |
| Universe | 8 symbols (AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, SPY); expansion steps for 20–30 in `config/universe.yaml`. | Start small, keep the data honest. |
| Sealed testbed | Assumed 2026-07-01 → 2026-07-21 in `config/splits.yaml`. Editable; nothing hardcodes it. | Last complete interval in the collected range. |
| Engine choice | Local broker behind the JSON contract. HKUDS AI-Trader / Backtrader not adopted. | The local broker already reproduces every check; an external engine would have to match it. |

Still later (unchanged): Zetaris publish of the released feed, Nemotron policy, Meterless, checkpoint restore, live mode, ablations.

## What this component is

The simulator is the market environment and the only official scorer. It owns the clock, which records are released, fills, costs, positions, and the performance report. Trading agents never import it, and it never imports an agent. A runner connects them through versioned JSON.

It must run with a scripted agent and no Meterless, no Nemotron, and no Zetaris. Those systems attach later. Zetaris is where the full agent will read the released feed. The simulator may replay its private archive on its own.

Build target for the first slice: Phase 2 of the build guide, with the smallest data-release surface Phase 1 needs so a scripted agent can be checked. Do not start Meterless, Nemotron, or the ablation harness.

## Boundary

```text
scripted agent  --JSON-->  runner  --JSON-->  simulator
                                |
                                +--> private snapshot (prices, news, context)
                                +--> released feed for this run and this as_of
                                +--> account ledger (orders, fills, cash, positions)
                                +--> official report
```

Later, the same released feed is published into Zetaris under a simulator identity. The agent SQL identity can read that feed and write only its own workspace. The private archive, including future rows, stays unreachable.

`sim/` must not import `agent/`. `agent/` must not import `sim/`.

## Run lifecycle

`reset(run_config) -> decision_request`

`submit_action(action_json) -> acknowledgement`, then the simulator advances events and returns the next decision request, or an end-of-run report.

One run:

1. Load a pinned dataset version, start and end, `initial_cash = K`, costs, position limits, mode (`V1` or `V2`), cadence, and `time_scale`.
2. Set cash to `K`, positions to the configured start (empty unless the config says otherwise), and new `run_id`s. Do not reset cash at each minute.
3. At each simulated-minute boundary, release every record eligible at `as_of`, then emit one decision request. Pin that request's cutoff even if more data arrives while the agent is thinking.
4. Validate the single JSON response. Accept it, or reject it and record why.
5. Advance market and news events until the next boundary. Activate orders when they become due. Fill from the execution model, not from a price the agent named.
6. At the end, emit the official report from the ledger. Agent notes cannot change the score.

Historical replay pauses the decision clock during inference and keeps the SQL cutoff fixed, then resumes. Live paper mode is out of the first slice: `time_scale = 1`, real latency counts, and late plans are rejected rather than backdated.

## Clock

| Name | Meaning |
| --- | --- |
| `initial_cash = K` | Starting money. Not a time setting. |
| `time_scale = s` | Simulated seconds per playback second. Visible on the request. `60` means 60x. Live mode uses `1`. |
| `cadence_clock` | `simulated`. One decision request per 60 simulated seconds. |
| `decision_interval_seconds` | `60`. |
| `execution_resolution_ms` | Must match the data. Do not claim second fills from minute bars. |
| V1 activation | `execute_at` equals the decision boundary (`as_of` / `window_start`). |
| V2 activation | `execute_at` is in `[window_start, window_end)`. The agent picks times inside that minute. |

A V2 plan cannot grow a new model decision when news arrives mid-minute. The article enters the feed for the next request. A scheduled order still activates on its accepted time.

V1 needs minute data. V2 needs quotes or trades fine enough for the claimed resolution. A completed bar's high, low, close, and volume are not known at the open of that bar. A chosen timestamp fills on the first suitable market event under the execution model.

## Information release

For a decision at time `t`, a news or context record is eligible only when all of these hold:

- `published_at <= t`
- `available_at <= t`
- `released_at <= t`

`retrieved_at` is when we collected it. It does not make a record historically available. A later edit is a new version with its own times. Reject records whose historical availability cannot be established.

The same cutoff applies to prices, derived features, joined statistics, and anything later stored as memory. The agent must not be trusted to add its own date filter. Release objects and permissions are the boundary.

Worked example from the design, to encode as a test:

- AAPL article published `14:00:35`, available `14:00:40`. Absent from the `14:00:00` request. Released at `14:00:40`. Usable at the `14:01:00` decision.
- An article at `14:02` stays private until its own release time.
- In V2, an order scheduled at `14:00:45` still follows the plan accepted at `14:00:00`. The `14:00:40` article does not create another decision.

## JSON contract

Decision requests are `schema_version` `1.1`. Action responses and acknowledgements stay `1.0`.

Decision request fields: `schema_version`, `run_id`, `request_id`, `mode`, `as_of`, `window_start`, `window_end`, `initial_cash`, `account`, `clock` (`cadence_clock`, `decision_interval_seconds`, `time_scale`, `execution_resolution_ms`), `data` (`feed_schema`, `workspace_schema`, `news_event_ids`).

`account` fields: `currency`, `cash`, `equity`, `positions`, `deposit_today`, `total_deposits`, `interest_earned`, `cash_annual_rate`. The simulator credits `deposit_today` at the start of each trading day before the decision, and credits cash interest (`cash * cash_annual_rate / 252`) at day close. The agent cannot create cash. `total_deposits` does not include initial cash.

Action response fields: `schema_version` (`1.0`), `run_id`, `request_id`, `mode`, `window_start`, `window_end`, `orders`, `reason`.

Each order: `order_id`, `execute_at`, `symbol`, `side` (`BUY` or `SELL`), `quantity`, `order_type` (`MARKET` for the first slice; `limit_price` is null on market orders), `limit_price`.

Validation rejects the response when any of these fail:

- Not a single JSON object, or keys and types do not match.
- `run_id`, `request_id`, `mode`, or window do not match the prompt.
- Unknown symbol, non-positive or non-finite quantity, bad timestamp.
- V1 `execute_at` is not the decision boundary. V2 `execute_at` is outside `[window_start, window_end)`.
- Duplicate `order_id`, or a second submission for the same request.
- Order count above the configured bound.
- At execution, cash, position, or shorting rules fail. Record the rejection. Do not carry an expired plan into the next minute.

`orders: []` is hold. `reason` is recorded and is not used for scoring.

Acknowledgement reports accepted and rejected orders. The next request carries the updated account. Fills include the simulator's price, costs, and time. The agent does not supply the fill price.

## Accounting and report

Configure commissions, slippage, market hours, cash constraints, and position limits. No deposits or withdrawals.

`net_profit = final_equity - K`, after costs, with one consistent mark for open positions.

The report is produced from the ledger. Minimum fields for a run record: `run_id`, `initial_cash`, `final_equity`, `net_profit`, `max_drawdown`, fills, rejections, turnover, `environment_version`, `data_version`. Sharpe can wait until a longer run exists; if computed, state the sampling interval.

## Engine choice

Not pinned. HKUDS AI-Trader is the first candidate to inspect. Backtrader is the documented local fallback. Until an adapter reproduces the checks below, keep a small local broker behind the same JSON contract so the scripted tests do not wait on that choice. Recording a caller-supplied trade price does not count as execution.

## First-slice layout

Only what the simulator needs. Leave `agent/`, Meterless, and Zetaris clients for later, except a scripted caller in tests.

```text
ai-trader/
├── config/environment.yaml    # K, mode, cadence, time_scale, costs, bounds
├── contracts/
│   ├── decision_request.py
│   ├── action.py
│   └── report.py
├── sim/
│   ├── snapshot/              # private archive, may contain future rows
│   ├── news_queue.py
│   ├── feed.py                # release by as_of; Zetaris publish comes later
│   ├── scheduler.py
│   ├── broker.py
│   ├── engine.py
│   ├── metrics.py
│   └── service.py             # reset / submit_action / report
├── runner.py
└── tests/                     # scripted agent, no model
```

## Checks that define done

Run these with a scripted agent and a hand-computed toy book:

1. Known orders change cash and positions by the configured costs.
2. Two V2 orders at different times in the same minute fill in that order, on the first suitable event.
3. The `14:00:40` article is absent at `14:00:00` and present at `14:01:00`. The `14:02` article is still absent at `14:01:00`.
4. Malformed JSON, a late V2 timestamp, an unknown symbol, and a duplicate submission are rejected and recorded.
5. A sell is rejected when inventory is insufficient and shorting is off.
6. Replaying the same accepted action log on the same data and config reproduces the same account.
7. The simulator process does not import an agent, call a model, or read a future row while serving `as_of`.

Reset isolation (a test-only memory marker must disappear on the next run) belongs to the evaluation harness, not this slice. Note it so the ledger and run IDs stay per-run and disposable.

## Explicitly later

- Zetaris registration and the four data products (`decision_context`, `strategy_performance_by_context`, `strategy_lineage_evidence`, `evaluation_report`).
- `zetaris_query`, agent workspace tables, and permissioned identities.
- Nemotron and the fixed-model action policy.
- Meterless planning, memory, and strategy search.
- Sealed testbed, checkpoint restore, and the ablation matrix.
- Live research agent and wall-clock latency.
- Iteration-versus-profit plots. No fabricated results.
