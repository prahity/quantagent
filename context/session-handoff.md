# Resume here

Read this file, then `meterless/README.md`. The market environment is `simulator/README.md`. The long design is `README.md` at the repo root.

Git remote: `https://github.com/prahity/quantagent.git` on `main`.

## Where the work stopped

The simulator replays real Yahoo bars and SEC filings from 2024-01-02 through 2026-07-21. The trading model is `NemotronTrader` in `meterless/trader.py`. It calls Nightshift. The model replies with orders and a short reason. `meterless/action.py` turns that reply into the simulator's `schema_version` `1.0` action:

- `structure_action` copies `run_id`, `request_id`, `mode`, `window_start`, and `window_end` from the request. Extra fields are dropped. Symbol and side are uppercased. A V1 order's `execute_at` is `window_start`. An order the schema cannot keep is dropped, and the rest of the reply is still submitted.
- `fit_orders` sizes from the latest completed close. A buy is shrunk so it fits in cash, with a small cushion for slippage, and so the position stays within `max_position_pct_equity`. A sell is shrunk to shares already held. An order that shrinks to zero shares is dropped.

A reply that is not a JSON object is a hold. The model does not set the fill price. The prompt's goal is to end the window with more cash. Cash and open positions are both counted at the final close, and cash by itself earns nothing.

`simulator/trial.py` compares two arms on daily bars for AAPL, MSFT, and SPY, one decision per completed day, $10,000 at the start of each window:

- `nomem` sees only that day's request. All 31 months from 2024-01 through 2026-07 are in `context/trials/monthly-2024-2026/results.jsonl`. A month takes about 20–36 minutes. Six months run at once, so twelve months of wall clock is about an hour.
- `memory` keeps a short order log and a strategy notebook rewritten after each window. January–July 2024 finished under `context/trials/monthly-2024-2026/memory-attempt-1/`. Those notes invent figures the simulator did not report. Do not treat them as evidence. The run is stopped.

`--days 30 --start 2024-01-16` runs one window of that many calendar days instead of the month loop. The window id is `2024-01-16+30d`. Each decision is printed as the run goes.

The notebook is `meterless/notebook.py`. An amend whose id is not already in the notebook is saved as a new rule, so the first window can start from an empty file. H-MEM is the memory layer and is not wired in. The notebook is the temporary memory until that integration replaces it. A figure in an order reason is kept only when it appears in the SQL rows or the bars the simulator already released.

Analysis SQL is `meterless/query.py`. The agent names `bars` and `filings`. The adapter binds the simulator's `as_of`, so a date in the SQL cannot see a later row. `ANALYSIS_BACKEND=sqlite` is the local snapshot. `ANALYSIS_BACKEND=zetaris` is the same call against `ZETARIS_QUERY_URL`. The swap steps are in `meterless/README.md`. Only the memory arm queries SQL. Each day gets at most one SQL call, then the order. Full query rows append to `queries.jsonl` in the trial output.

Pinned model for both arms: `nvidia/nemotron-3.5-lightning-30b-a3b`, with reasoning off (`--thinking off`). That is the small Nemotron lane. `meterless/cheap`, `meterless/local`, and `webllm` are faster and miss the action JSON more often. `meterless/auto` can change lanes between calls, so the trial passes the pinned id.

## What a clone does not include

Git leaves these out. Recreate them on the new machine. A token belongs in `.env` or in Nightshift, and nowhere else.

| Left out | What it is | How to get it |
| --- | --- | --- |
| `.venv/` | Python packages | `python3.12 -m venv` and `pip install`, below |
| `.env` | Nightshift runtime token and optional backend settings | `cp .env.example .env`, then fill the token |
| `simulator/sim/snapshot/*.parquet` | Yahoo prices and SEC filings | `python scripts/fetch_data.py` |
| `simulator/sim/snapshot/analysis.sqlite` | SQL database the memory arm reads | Built on the first memory-arm run from the parquet files |
| `simulator/runs/` | Per-run requests, actions, fills, and reports | Written when the simulator or a trial runs |
| `simulator/web/node_modules/` and `simulator/web/.next/` | Web UI packages and build cache | `npm install` in `simulator/web` |
| `__pycache__/`, `*.pyc`, `.pytest_cache/` | Python bytecode and test cache | Created by Python and pytest |

### Python environment

From the repo root. Python 3.12. The simulator package asks for 3.11 or newer.

```bash
cd <this repo>
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e "./simulator[dev]"
```

`simulator/README.md` also shows a venv made with `uv` inside `simulator/`. Use one environment. The trial commands below assume the repo-root `.venv` is active.

### Nightshift and `.env`

Nightshift is a separate app. It is not in this repository. Install the Mac, Windows, or DGX edition from [meterless.ai](https://meterless.ai/) and leave it running on this machine. The app calls `http://127.0.0.1:8088/v1`. A cloud workspace cannot open that port. The process name is `nightshif`.

The NVIDIA API key goes in Nightshift, Setup, Providers, NVIDIA. It does not go in `.env`, code, or git. The app's secret is the Nightshift Console runtime token. The Console can also show a connect link of the form `http://127.0.0.1:8088/connect#k=<token>`. Copy the token into `.env`. Do not print it, and do not commit `.env`.

```bash
cp .env.example .env
```

```bash
METERLESS_BASE_URL=http://127.0.0.1:8088/v1
METERLESS_RUNTIME_TOKEN=<Console API key>
METERLESS_MODEL=meterless/auto
ANALYSIS_BACKEND=sqlite
```

`unauthorized` means Nightshift answered and rejected the token. `offline` means it did not answer with Meterless model ids. The probe never echoes the token.

```bash
set -a && source .env && set +a
python -m meterless
python -m meterless --say "Reply with one sentence confirming this connection works."
```

The model list should include `meterless/auto` and `nvidia/nemotron-3.5-lightning-30b-a3b`. Header on every call is `meterless-app: ai-trader`, set in `meterless/provider.py`.

Zetaris is optional. Leave `ANALYSIS_BACKEND=sqlite` until those views exist. The variables to fill, and the HTTP shape the adapter posts, are in `meterless/README.md`: `ZETARIS_QUERY_URL`, `ZETARIS_TOKEN`, `ZETARIS_BARS_VIEW`, `ZETARIS_FILINGS_VIEW`, `ZETARIS_DATA_VERSION`.

### Market snapshot

`simulator/sim/snapshot/` is gitignored except `.gitkeep`. Prices come from the Yahoo Finance chart API. Filings come from the SEC EDGAR submissions API. Neither source needs an API key. The SEC asks for a contact in the User-Agent. That string is not a secret.

```bash
export SEC_USER_AGENT="Your Name you@example.com"
cd simulator
python scripts/fetch_data.py
```

The fetch writes `bars_daily.parquet`, `bars_hourly.parquet`, and `news.parquet`, and it rewrites `simulator/config/snapshot_manifest.json`. The committed `data_version` is `snap-d5b9a15c35433e85`. A later fetch can produce a different id when Yahoo or EDGAR has changed a row. Keep that local manifest with the parquet files you just downloaded. The universe is AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, and SPY, from 2024-01-01 through 2026-07-21 (`simulator/config/universe.yaml`). Yahoo serves hourly history for about the last 730 days, and the collector requests that range and filters it. Daily bars are what the trial uses.

Two optional news providers are skipped unless their keys are set in the environment before `fetch_data.py`. The committed snapshot was built without them.

```bash
export ALPHAVANTAGE_API_KEY=<free key from alphavantage.co>
export FINNHUB_API_KEY=<free key from finnhub.io>
```

`python scripts/fetch_data.py --fixtures` also refreshes the small committed slices in `simulator/tests/fixtures/`. Pytest uses those fixtures and does not need the full snapshot or the network.

The first memory-arm run builds `simulator/sim/snapshot/analysis.sqlite` from the parquet files. It is gitignored with the rest of the snapshot. If the parquet files are missing, that run stops and names `fetch_data.py`.

### Simulator logs and the web UI

`simulator/runs/<run_id>/` appears when the API, `runner.py`, or a trial runs. Each directory holds `config.json`, `requests.jsonl`, `actions.jsonl`, `fills.jsonl`, `rejections.jsonl`, `equity.jsonl`, and `report.json`. There is nothing to download.

The web UI needs Node.js, then its packages:

```bash
cd simulator
.venv/bin/uvicorn server.app:app --port 8711 --reload

cd web
npm install
npm run dev -- --port 3177
```

Open http://127.0.0.1:3177. `node_modules/` and `.next/` stay out of git.

## Continue the trial

From the repo root, with the venv and `.env` loaded, and with the snapshot fetched:

```bash
cd simulator
PYTHONUNBUFFERED=1 PYTHONPATH=.. python -u trial.py \
  --out ../context/trials/monthly-2024-2026 \
  --thinking off \
  --model nvidia/nemotron-3.5-lightning-30b-a3b
```

Months already listed in `results.jsonl` are skipped. The no-memory arm will skip all 31 months. The memory arm starts at 2024-01 in this output directory, because the earlier memory rows live under `memory-attempt-1/` and used the old notebook. To rerun one arm only, pass `--arms nomem` or `--arms memory`. To cover one year, pass `--first 2024-01 --last 2024-12`.

One 30-day window, which is the short check that the action repair and the notebook update both run:

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=.. python -u trial.py \
  --out ../context/trials/30d \
  --days 30 \
  --start 2024-01-16 \
  --thinking off \
  --model nvidia/nemotron-3.5-lightning-30b-a3b
```

A hold-only month with `invalid_responses` near the number of trading days means the model missed the JSON object. Fix the parse or the prompt before treating that score as a trading result. The reasoning-off file `context/trials/monthly-2024-2026-nothink/results.jsonl` is that kind of run: March–December 2024, no fills. An order that `fit_orders` shrinks to zero shares is submitted as a hold and does not increment `invalid_responses`.

Checks that do not call the model:

```bash
python -m pytest -q meterless/tests
cd simulator && python -m pytest -q
```
