# Simulator work context

Handoff for the simulator that was built in this folder. The design documents at the repository root (`README.md`, `AI-Trader-Architecture.md`, `AI-Trader-Build-Guide.txt`) describe the full Zetaris / Meterless / Nemotron agent. This file describes only the environment that was implemented, the choices behind it, and the parts that are still later. The original working spec, including the checks that defined "done", is `context/simulator.md`. How to run it is `README.md` in this folder.

## Where it sits

```text
quantagent/
├── README.md, AI-Trader-Architecture.md, AI-Trader-Build-Guide.txt   agent design, not built
└── simulator/                                                         this work
    ├── sim/            clock, snapshot, feed, broker, scheduler, engine, ledger
    ├── contracts/      schema_version 1.0 request, action, acknowledgement, report
    ├── agent/          deterministic test harness only (hold, buy_and_hold, JSON scripts)
    ├── runner.py       the only module that imports both sim and agent
    ├── server/         FastAPI, default port 8711
    ├── web/            Next.js UI, default port 3177, proxies /api/* to the API
    ├── scripts/fetch_data.py
    ├── config/         environment, universe, splits, committed snapshot manifest
    ├── tests/          29 checks on a real slice of the snapshot
    └── runs/           per-run logs, gitignored
```

`sim/` does not import `agent/` or `server/`. `agent/` does not import `sim/` or `server/`. `tests/test_boundaries.py` checks that with the AST. The Nemotron trading agent is not in `agent/`. That package is a scripted stand-in so the environment can be checked without a model.

## What a run does

`SimulatorService.reset` opens an isolated account at cash K (default 10,000 USD), empty positions, and a new `run_id`. It does not reset cash on later bars. One decision request is issued per completed bar. The agent returns one JSON object. `orders: []` is a hold. The simulator validates it, activates due orders, fills them from the bar data, and either emits the next request or the end-of-run report.

The report is computed from the ledger. `net_profit = final_equity - K` after costs. Open positions are marked at the last completed bar's close. Agent text in `reason` is stored and does not affect the score.

While the API process is running, the live account is in memory. Restarting the process drops it. The files under `runs/<run_id>/` remain:

| File | Contents |
| --- | --- |
| `config.json` | Symbols, interval, mode, window, K, costs |
| `requests.jsonl` | Each decision request, including `as_of` and the released news ids |
| `actions.jsonl` | The JSON sent back, plus the acknowledgement |
| `fills.jsonl`, `rejections.jsonl`, `equity.jsonl`, `report.json` | Written when the run finishes |

Replay builds a fresh engine from `config.json` and `actions.jsonl` and compares `final_cash`, `final_equity`, `net_profit`, `fills`, `rejections`, and `max_drawdown_pct` with `report.json`.

## Data

No synthetic prices or filings. `scripts/fetch_data.py` writes `sim/snapshot/` (gitignored) and `config/snapshot_manifest.json` (committed). The built snapshot is `snap-17f602c41277a742`.

| Source | What was collected | Key |
| --- | --- | --- |
| Yahoo Finance chart API | Hourly and daily bars, 2024-01-02 through 2026-07-21 | None. A browser User-Agent only. |
| SEC EDGAR submissions API | 261 filings for the universe | None. `SEC_USER_AGENT` is a contact string, not a secret. |
| Alpha Vantage `NEWS_SENTIMENT` | Not in this snapshot | Only if `ALPHAVANTAGE_API_KEY` is set |
| Finnhub company news | Not in this snapshot | Only if `FINNHUB_API_KEY` is set |

Universe in `config/universe.yaml`: AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, SPY. Each symbol has 639 daily bars and 4,446 hourly bars. SPY has no matching filings in this pull. SEC forms kept are 8-K, 8-K/A, 10-Q, 10-Q/A, 10-K, 10-K/A, N-CSR, N-CSRS. Titles are the SEC item names, not headlines. The Microsoft line "Departure/Election of Directors or Officers; Compensatory Arrangements" is item 5.02 of a real 8-K (22 January 2025, accession `0001193125-25-010492`).

`tests/fixtures/` is a real cut of that snapshot (AAPL, MSFT, SPY, 2025-01-27 through 2025-02-07), so `pytest` does not use the network.

Nemotron web search is not a historical source. It is reserved for a later live-mode research agent.

## What the agent is allowed to see

For a decision at `as_of`:

- A bar is visible only after it has completed (`bar_end <= as_of`). The high, low, close, and volume of the current bar are not known at its open.
- A filing is visible only when `published_at`, `available_at`, and `released_at` are all `<= as_of`.
- `published_at` for EDGAR is the acceptance time converted to real UTC. The submissions API labels Eastern local time as `Z`. `sec_acceptance_to_utc` corrects that. `acceptance_api_raw` is kept.
- If acceptance was after hours and EDGAR assigned the next filing date, `available_at` is 06:00 America/New_York on that filing date.
- Asking the feed for an `as_of` past the run clock raises `FutureAccessError`, which the API returns as HTTP 403.

The per-request digest lists up to 20 released event ids, holdings first.

## Orders and fills

V1: `execute_at` must equal the decision boundary. V2: `execute_at` may be any time in `[window_start, window_end)`. Fills happen at the open of the first bar whose start is `>= execute_at`, plus `slippage_bps` against the trader (default 1 bp). Buys pay more, sells receive less. Default commission is 0. Shorting is off. A position cannot exceed 25% of equity at execution. `LIMIT` orders wait until marketable and expire after `order_validity_bars` (default 1). Unfilled orders are not silently carried. These defaults live in `config/environment.yaml` and can be overridden per run.

Cadence is one request per hourly or daily bar, chosen per run. `decision_interval_seconds` on the request reports the real bar length (3600 or 86400). The design's one-request-per-simulated-minute was not implemented: free, verifiable minute bars for 2024–2026 were not available, and inventing minute timestamps on top of hourly bars would fake the resolution.

## Contract

`schema_version` is `1.0`. A response is one JSON object. Validation rejects the whole response, and the simulator records a hold, when the payload is not an object, ids or the window do not match the request, the symbol is unknown, the quantity or timestamp is bad, V1/V2 timing is wrong, `order_id`s repeat, or the order count exceeds the limit. At execution, insufficient cash, insufficient inventory, and the position cap are recorded rejections. A limit that is not yet marketable is not a final rejection. It waits.

`POST /api/runs/{id}/actions` must read the body with FastAPI `Body`. A bare `payload: Any = None` binds a query parameter, so the simulator sees `null` and records `not_an_object`. That bug was hit from the web order form and is covered by `tests/test_api.py`.

## UI

`web/` is a client of the API. It does not read the snapshot itself. Pages:

- `/` creates a run (interval, mode, dates, symbols, K, costs) and lists runs.
- `/runs/<id>` shows the released price feed, fills, equity and drawdown, the news digest, manual orders, test-agent autoplay, rejections, the decision log, and the report with a replay check. "Try reading one week ahead" is supposed to come back 403.
- `/data` shows the universe, splits, coverage, and news sources.
- The header link "API" opens `/api/config`, the raw JSON the UI is reading. It is not a separate product surface.

## Decisions that differ from the design docs

| Design | Built | Reason |
| --- | --- | --- |
| One decision per simulated minute | One decision per completed hourly or daily bar | No honest minute data for the full window |
| V2 fills inside the minute from trades or quotes | V2 `execute_at` fills at the next bar open | Do not claim a finer price than the bar |
| Market orders only | Market and limit | Limits exercise wait and expiry |
| HKUDS AI-Trader or Backtrader | Local broker behind the same JSON contract | It already passes the done-checks |
| Universe of many names | 8 names, with expansion steps in `config/universe.yaml` | Grow it before early-2024 hourly bars age out of Yahoo's ~730 day window |
| Sealed testbed chosen before development | `config/splits.yaml` uses 2026-07-01 through 2026-07-21 | Assumption. Edit the file. Nothing else hardcodes those dates. |

## Quirks worth not rediscovering

- Yahoo hourly history with an explicit old `period1` returns 422. The collector requests `range=730d` and filters locally.
- Pandas 3 round-trips parquet timestamps at microseconds. The store normalizes them to nanoseconds. Mixing resolutions makes "no decision boundaries" or breaks equality checks.
- The EDGAR acceptance fix was checked against filing index pages in both EST and EDT. Do not "correct" it by trusting the `Z` suffix.
- `sim/snapshot/` and `runs/` are gitignored on purpose. After a fresh clone, run `python scripts/fetch_data.py` once. The manifest says which `data_version` to expect.

## Explicitly not built

Zetaris registration and the four data products, `zetaris_query`, a Nemotron action policy, Meterless planning and memory, checkpoint restore, the sealed-test ablation matrix, live paper trading, and a research agent with web search. The simulator may later publish the already-released feed into Zetaris. It must not hand the agent the private snapshot, including future rows.
