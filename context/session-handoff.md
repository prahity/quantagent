# Resume here

Read this file, then `meterless/README.md`. The market environment is `simulator/README.md`. The long design is `README.md` at the repo root.

Git remote: `https://github.com/prahity/quantagent.git` on `main`.

## Where the work stopped

The simulator replays real Yahoo bars and SEC filings from 2024-01-02 through 2026-07-21. The trading model is `NemotronTrader` in `meterless/trader.py`. It calls Nightshift and must answer with `schema_version` `1.0` action JSON. Anything else is a hold. The model does not set the fill price.

`simulator/trial.py` compares two arms on daily bars for AAPL, MSFT, and SPY, one decision per completed day, $10,000 at the start of each month:

- `nomem` sees only that day's request. All 31 months from 2024-01 through 2026-07 are in `context/trials/monthly-2024-2026/results.jsonl`. A month takes about 20–36 minutes. Six months run at once, so twelve months of wall clock is about an hour.
- `memory` keeps a short order log and a strategy notebook rewritten after each month. January–July 2024 finished under `context/trials/monthly-2024-2026/memory-attempt-1/`. Those notes invent figures the simulator did not report. Do not treat them as evidence. The run is stopped.

Analysis SQL is `meterless/query.py`. The agent names `bars` and `filings`. The adapter binds the simulator's `as_of`, so a date in the SQL cannot see a later row. `ANALYSIS_BACKEND=sqlite` is the local snapshot. `ANALYSIS_BACKEND=zetaris` is the same call against `ZETARIS_QUERY_URL`. The swap steps are in `meterless/README.md`. Only the memory arm queries SQL. Each day gets at most one SQL call, then the order. Full query rows append to `queries.jsonl` in the trial output.

H-MEM is the memory layer and is not wired in. `meterless/notebook.py` is the temporary notebook until that integration replaces it. A figure in an order reason is kept only when it appears in the SQL rows or the bars the simulator already released.

Pinned model for both arms: `nvidia/nemotron-3.5-lightning-30b-a3b`, with reasoning off (`--thinking off`). That is the small Nemotron lane. `meterless/cheap`, `meterless/local`, and `webllm` are faster and miss the action JSON more often. `meterless/auto` can change lanes between calls, so the trial passes the pinned id.

## Set up Meterless

Nightshift has to be running on this machine. The app calls `http://127.0.0.1:8088/v1`. A cloud workspace cannot open that port. The process name is `nightshif`.

The NVIDIA API key goes in Nightshift, Setup, Providers, NVIDIA. It does not go in `.env`, code, or git. The app's secret is the Nightshift Console runtime token.

```bash
cd <this repo>
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e "./simulator[dev]"
cp .env.example .env
```

Fill `.env` from the Nightshift Console. Do not print the token.

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

The price snapshot is gitignored. After a fresh clone, rebuild it before a trial. Expected manifest id: `snap-17f602c41277a742`.

```bash
export SEC_USER_AGENT="Your Name you@example.com"
cd simulator
python scripts/fetch_data.py
```

The first memory-arm run builds `simulator/sim/snapshot/analysis.sqlite` from that snapshot. It is gitignored.

## Continue the trial

From the repo root, with the venv and `.env` loaded:

```bash
cd simulator
PYTHONUNBUFFERED=1 PYTHONPATH=.. python -u trial.py \
  --out ../context/trials/monthly-2024-2026 \
  --thinking off \
  --model nvidia/nemotron-3.5-lightning-30b-a3b
```

Months already listed in `results.jsonl` are skipped. The no-memory arm will skip all 31 months. The memory arm starts at 2024-01 in this output directory, because the earlier memory rows live under `memory-attempt-1/` and used the old notebook. To rerun one arm only, pass `--arms nomem` or `--arms memory`. To cover one year, pass `--first 2024-01 --last 2024-12`.

A hold-only month with `invalid_responses` near the number of trading days means the model missed the JSON contract. Fix the parse or the prompt before treating that score as a trading result. The reasoning-off file `context/trials/monthly-2024-2026-nothink/results.jsonl` is that kind of run: March–December 2024, no fills.

Checks that do not call the model:

```bash
python -m pytest -q meterless/tests
cd simulator && python -m pytest -q
```
