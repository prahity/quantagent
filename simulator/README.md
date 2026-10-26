# Simulator

The independent market environment for AI Trader. It owns the clock, which records are released, fills, costs, positions, and the official performance report. The Zetaris, Meterless, and Nemotron agent lives outside this folder.

`agent/` here is the deterministic test harness (`hold`, `buy_and_hold`, and JSON scripts). It is not the trading agent. `sim/` never imports `agent/`, and `agent/` never imports `sim/`. `runner.py` is the only piece that connects them.

Run every command below from this folder. After a clone, `sim/snapshot/`, `runs/`, `.venv/`, and `web/node_modules/` are absent. The commands that rebuild them are in [`../context/session-handoff.md`](../context/session-handoff.md).

| Path | Role |
| --- | --- |
| `sim/` | Snapshot store, as-of gated feed, news queue, broker, V1/V2 scheduler, engine, metrics, service. |
| `contracts/` | Fixed JSON contract (`schema_version` 1.0): decision request, action validation, acknowledgement, report. |
| `agent/` | Deterministic test agents and `agent/scripts/`. |
| `runner.py` | CLI that connects a test agent to the simulator, in-process or over HTTP, and replays recorded runs. |
| `server/` | FastAPI runner layer. |
| `web/` | Next.js interface for runs, the released feed, manual orders, the test agent, and reports. |
| `scripts/fetch_data.py` | Collects real data into `sim/snapshot/` (gitignored) and writes `config/snapshot_manifest.json`. |
| `config/` | Cash, costs, limits, universe, chronological splits, snapshot manifest. |
| `tests/` | Acceptance checks on real-data fixtures. |
| `context/simulator.md` | Working notes for this component. |

## Data

No synthetic data. The snapshot covers 2024-01-01 → 2026-07-21 for the universe in `config/universe.yaml`:

- **Prices:** Yahoo Finance chart API, hourly and daily bars. No key. Hourly history is only served for the trailing ~730 days, so the collector fetches `range=730d` and filters locally.
- **News/filings:** SEC EDGAR submissions API. No key. Set `SEC_USER_AGENT="Your Name you@example.com"` because the SEC asks for a contact in the User-Agent. Each filing keeps its acceptance time, a conservative `available_at`, the raw API timestamp, and an availability basis.
- **Optional extra news:** set `ALPHAVANTAGE_API_KEY` and/or `FINNHUB_API_KEY` (free tiers) and the collector adds those articles. If the variables are unset, those providers are skipped. Nemotron web search is reserved for the later live-mode research agent.

```bash
uv venv .venv --python 3.12 && uv pip install -e ".[dev]"
export SEC_USER_AGENT="Your Name you@example.com"
.venv/bin/python scripts/fetch_data.py
.venv/bin/python scripts/fetch_data.py --fixtures
```

## Run it

```bash
.venv/bin/uvicorn server.app:app --port 8711 --reload

cd web && npm install && npm run dev -- --port 3177

.venv/bin/python runner.py scripts
.venv/bin/python runner.py run --agent script --script aapl_round_trip --start 2025-01-27 --end 2025-01-31 --symbols AAPL,MSFT
.venv/bin/python runner.py run --agent buy_and_hold --interval daily --start 2025-01-02 --end 2025-03-31
.venv/bin/python runner.py replay <run_id>

.venv/bin/pytest -q
```

The web app proxies `/api/*` to the API (`SIM_API_URL`, default `http://127.0.0.1:8711`).

Each run writes `runs/<run_id>/` with `config.json`, `requests.jsonl`, `actions.jsonl`, `fills.jsonl`, `rejections.jsonl`, `equity.jsonl`, and `report.json`. Replay re-executes the recorded actions on a fresh engine and must reproduce the report.

## Behaviour

- One decision request per completed bar (hourly or daily, chosen per run). A bar is visible only once complete. News is visible only once `published_at`, `available_at`, and `released_at` are all ≤ `as_of`. A future `as_of` returns HTTP 403.
- V1 orders execute at the decision boundary. V2 orders carry an `execute_at` inside the window and fill, in time order, at the open of the first bar starting at or after that time.
- Fills use the next bar open, plus `slippage_bps`, `commission_per_share` / `commission_min`, no shorting by default, `max_position_pct_equity`, and `order_validity_bars`. `LIMIT` orders wait until marketable. Defaults are in `config/environment.yaml` and can be overridden per run.
- The report is `net_profit = final_equity - K` after costs, with max drawdown, Sharpe (sampling interval stated), turnover, fills, rejections, `environment_version`, and `data_version`.

## Publishing the released feed to Zetaris

The simulator owns released-feed writes. `scripts/publish_zetaris.py` loads the whole frozen snapshot into Zetaris once, under the simulator login. Each row carries a hidden `_released_epoch`: bars are released at `bar_end`, and news at `max(published_at, available_at)`. The agent's query tool shows a row only once that time is at or before the decision's `as_of`, which gives the same visibility as the run's own feed at every decision boundary (`tests/test_publish.py`). The one cosmetic difference is the news `released_at` value: Zetaris shows when the record became eligible, not the boundary at which a particular run released it.

1. Sign in to the web UI as an admin and run `config/zetaris_setup.sql` in its SQL editor. It creates a lakehouse, the feed tables, and a read-only `aitrader_agent` login (SELECT only). The simulator publishes as the admin.
   - **Local Docker install (`zetaris-platform`):** the UI is at `localhost:3000`, but JDBC goes to the query engine at `jdbc:zetaris:lightning@localhost:10000`.
   - The driver jar is `zetaris-platform/jdbc/ndp-jdbc-driver-*.jar`, and logins are sign-in emails (for example `admin@zetaris.com`).
2. Fill in the `ZETARIS_*` settings in the repository-root `.env`, from `.env.example`. A JDK must be on PATH.
3. `python scripts/zetaris_probe.py` checks connectivity, parameter binding, the SQL dialect, and that the agent login cannot write.
4. `python scripts/publish_zetaris.py --create`. Add `--fixtures` for the small slice. `--replace` is required to switch `data_version`. If the driver rejects `?` parameters, use `--inline`.

The tables hold one `data_version` at a time, and the root runner refuses `--backend zetaris` unless it matches the snapshot being replayed.

## Expanding the universe to 20–30 symbols

Follow the steps at the top of `config/universe.yaml`: add the ticker, SEC CIK, and sector; re-run `scripts/fetch_data.py`; re-run `pytest`. Add symbols before early-2024 hourly bars leave Yahoo's 730-day window, and only use a symbol in run windows where it was actually listed under that ticker.
