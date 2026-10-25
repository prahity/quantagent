# Meterless Runtime

AI Trader calls Nightshift for Nemotron. The NVIDIA API key is entered in Nightshift's Providers tab. This package never reads it. To set this up on another machine and continue the trial, follow `context/session-handoff.md`.

Settings, from the Nightshift Console:

```bash
METERLESS_BASE_URL=http://127.0.0.1:8088/v1
METERLESS_RUNTIME_TOKEN=<console runtime token>
METERLESS_MODEL=meterless/auto
```

`meterless/auto` is a routing mode. The Nemotron model is whichever lane Nightshift assigns. Copy the URL the Console actually shows.

```bash
python -m meterless            # online, unauthorized, or offline
python -m meterless --detect
python -m meterless --say "Reply with one sentence confirming this connection works."
```

`unauthorized` means the runtime answered and rejected the token, including the placeholder `rt-local`. `offline` means it did not answer with Meterless model ids. A `/healthz` response alone is not treated as connected.

`NemotronTrader` asks the model for orders and a reason. `meterless/action.py` copies `run_id`, `request_id`, `mode`, and the window from the simulator request, drops extra fields, and shrinks a buy that would exceed cash or `max_position_pct_equity`. A reply that is not JSON is a hold. The model does not set the fill price.

## Swapping the SQL backend for Zetaris

Analysis is one call, `AnalysisSource.execute(QueryRequest)`, in `meterless/query.py`. The trader, the trial, and H-MEM talk only to that call. Replacing SQLite with Zetaris means configuring `ZetarisSource`. It does not mean editing the trader or the agent's SQL.

The agent always reads two relations:

```text
bars(symbol, interval, ts, open, high, low, close, volume)
filings(symbol, form, title, published_at, available_at)
```

`interval` is `daily` or `hourly`. Every timestamp is UTC text `YYYY-MM-DDTHH:MM:SSZ`, so text order is time order. A bar is visible only after `bar_end`. A filing is visible only when `published_at` and `available_at` are both at or before the cutoff. The simulator sets `as_of` on `QueryRequest`. The model cannot move it by writing a later date into the SQL.

Local backend, the default:

```bash
ANALYSIS_BACKEND=sqlite
```

`SqliteSource.open_snapshot` builds `simulator/sim/snapshot/analysis.sqlite` from the parquet snapshot. The agent-facing names `bars` and `filings` are temp tables filled with rows at or before `as_of`. The underlying tables are `price_history` and `filing_history`, and the SQLite authorizer refuses reads of anything else.

Zetaris backend:

```bash
ANALYSIS_BACKEND=zetaris
ZETARIS_QUERY_URL=https://<zetaris-host>/<sql-endpoint>
ZETARIS_TOKEN=<bearer token, omit if the endpoint has none>
ZETARIS_BARS_VIEW=released_bars
ZETARIS_FILINGS_VIEW=released_filings
ZETARIS_DATA_VERSION=<pinned source version>
```

`trial.py` calls `open_analysis_source(snapshot_dir=...)`. That function reads `ANALYSIS_BACKEND` and returns either source. `snapshot_dir` is used only for sqlite.

Register two Zetaris objects with the columns above. Their names must be identifiers and must not be `bars` or `filings`, because those are the names inside the query the adapter sends. The defaults are `released_bars` and `released_filings`. Store `bar_end` on the price object using the same rule as the simulator: the earlier of one bar-length after `ts` and the next bar's `ts` (86400 seconds for daily, 3600 for hourly).

`ZetarisSource.prepare` wraps the agent's statement so the model still writes `SELECT ... FROM bars` and `FROM filings`:

```sql
WITH bars AS (
  SELECT symbol, interval, ts, open, high, low, close, volume
  FROM released_bars WHERE bar_end <= '<as_of>'
),
filings AS (
  SELECT symbol, form, title, published_at, available_at
  FROM released_filings
  WHERE published_at <= '<as_of>' AND available_at <= '<as_of>'
)
<agent SELECT or WITH>
```

`execute` POSTs JSON to `ZETARIS_QUERY_URL`:

```json
{"sql":"<wrapped>","as_of":"2024-01-03T14:30:00Z","query_id":"...","goal":"...","run_id":"...","request_id":"..."}
```

The token, when set, is `Authorization: Bearer <ZETARIS_TOKEN>`. A 200 body is either `{"columns":[...],"rows":[{...}]}` or `{"error":"..."}`. Rows may be objects or arrays parallel to `columns`. More than 30 rows is truncated. This HTTP shape is the piece to change if the real Zetaris route differs. Change it inside `ZetarisSource.execute` only.

Leave these alone when swapping:

- Relation names in the prompt (`SCHEMA` in `query.py`). Both backends expose the same text.
- `QueryRequest.as_of`. It comes from the simulator decision, not from the model JSON.
- The trader. It calls `source.execute` and keeps `QueryResult`. It does not import sqlite or httpx.
- H-MEM. It is the memory layer. Every query is appended to `queries.jsonl` in the trial output. The notebook sees only a short excerpt of the last four.

Check the swap with `python -m pytest -q meterless/tests/test_query.py`. `test_zetaris_prepares_the_same_relations_and_the_server_cutoff` locks the wrapper and the cutoff. A future-bar test on the sqlite source locks the same rule.

## How long 12 months takes

Daily bars, AAPL, MSFT, and SPY, one decision per trading day, model `nvidia/nemotron-3.5-lightning-30b-a3b`. Measured from the 2024 months in `context/trials/monthly-2024-2026/`.

| Arm | What was measured | 12 months |
| --- | --- | --- |
| No memory | 12 months of 2024, one model call per day. Median month 27 minutes, range 20–36. Sum 5.4 hours. | About 5.5 hours one after another. The trial runs 6 no-memory months at once, so wall clock is about 1 hour. |
| Memory, before the cuts below | 7 months, one at a time, plus a notebook rewrite. January was 25 minutes. Later months were 51–99 minutes because the rewrite prompt ran past Nightshift's 150 second limit and retried. Sum 7.7 hours. | About 13 hours at that pace. |

The memory arm is sequential because month N's notes are an input to month N+1. The no-memory arm is not.

Lightning 30B is already the small Nemotron lane. A smaller lane (`meterless/cheap`, `meterless/local`, `webllm`) would return sooner and fail the action JSON more often. The speedups that keep this model are fewer calls and a shorter memory rewrite. Each day gets at most one SQL call before the order. The rewrite sees the month's totals, fills, and a short excerpt of the last four queries, and it retries once. A memory month that queries every day is about two model calls per day, so 12 months is on the order of 8–11 hours, against about 13 hours before these cuts and 12–18 hours if every day had made two SQL calls.
