# Exa market research for the Nemotron trader

Exa gathers company news and market context in an evaluator-side collection step.
Nemotron then receives a bounded set of released excerpts alongside prices and
filings through Meterless/Nightshift. The trading model cannot browse, choose its
own cutoff, or call Exa during a historical replay.

## Setup and collection

Use the existing Python environment; `httpx` and `pydantic` are already project
dependencies. From the repository root, copy `.env.example` to `.env` if needed,
set `EXA_API_KEY` in `.env`, and load it in your shell:

```bash
source .venv/bin/activate
set -a; source .env; set +a
python -m research collect --symbols AAPL,MSFT,SPY --lookback-days 7 --max-results 3
```

This makes one search per symbol, using names from
`simulator/config/universe.yaml`. Collection allows at most eight symbols and ten
results per symbol, has a 15-second timeout per search, and does not retry failed
searches automatically. Search calls may consume Exa credits. Successful results
survive another symbol's timeout or rate limit. An empty collection exits with
code 1; missing keys or invalid arguments exit with code 2. Warnings never include
provider response bodies or credentials.

Each collection is saved to an exclusive `research_archive/exa_<id>.json` file.
The directory is ignored by git. Records include the source URL, ticker, title,
captured excerpts, source-supplied publication metadata, retrieval time,
availability time, and collection release time. Excerpts are capped at 2,000
characters. No API key is included in the archive, a prompt, or a model response.
The collector requests Exa highlights rather than an additional synthesis model.

## Sort and filter by UTC day and hour

```bash
python -m research list --symbols AAPL --day 2026-10-26
python -m research list --symbols AAPL,MSFT --day 2026-10-26 --hour 14 --oldest-first
python -m research list --as-of 2026-10-27T00:00:00Z --limit 100
```

Dates above are examples; use publication dates present in your collection.
The list command is offline. Results are sorted by publication time, newest
first by default, and can be filtered by ticker, UTC day, UTC hour, and an as-of
cutoff. `published_day` is `YYYY-MM-DD`; `published_hour` is a UTC bucket such as
`2026-10-26T14:00:00Z`. Timezone offsets are normalized to UTC for grouping.

When Exa supplies only a date, the hour is `null`. Such a source appears in day
filters, never hour filters, and is withheld from decisions until the end of that
publication day. A timezone-free datetime is rejected rather than guessed.
Publication metadata is supplied by the source/provider; it is not independently
verified. Missing/invalid dates and URLs are excluded with a warning.

## Use the archive in trading

```bash
cd simulator
PYTHONPATH=.. ../.venv/bin/python runner.py run \
  --agent nemotron --model nvidia/nemotron-3.5-lightning-30b-a3b \
  --interval daily --start 2025-01-28 --end 2025-01-29 \
  --symbols AAPL,MSFT,SPY --research-archive ../research_archive
```

The normal simulator price snapshot and Nightshift runtime credentials must also
be configured, as described in `context/session-handoff.md`. Alternatively, set
`EXA_RESEARCH_ARCHIVE` to an **absolute directory path** in `.env`. Research is
disabled when neither that variable nor `--research-archive` is set.

Only articles whose publication, retrieval, availability, and release times are
all at or before the simulator's `as_of` enter the prompt. A collection must also
have completed by then. **Collecting a 2025 article today does not make its current
text eligible for a 2025 backtest.** Exa publication-date search filters are not
an archive of what that page looked like in the past. New collections are useful
for subsequent decisions on a matching market feed; this integration does not
add current market bars or a live brokerage connection. The bundled historical
simulator will normally exclude collections made today.

The trader sees at most six eligible excerpts from the previous seven days for
the run's tickers, in both no-memory and memory modes (SQL, brief, or notes).
It is told that external text is evidence rather than instructions and to cite
`source_id` when using it. Orders and execution prices remain subject to the
simulator's existing validation. This adapter does not register Exa data in
Zetaris; that remains a separate data-product integration.

For side-by-side trials, pass the same archive to both arms:

```bash
PYTHONPATH=.. ../.venv/bin/python trial.py --out ../context/trials/exa-integration \
  --first 2024-02 --last 2024-04 --research-archive ../research_archive
```

Use a fresh output directory when changing the archive. A trial refuses to mix
different archive manifests in an existing results file. Both arms receive the
same pinned collection bytes. Files added after run startup are ignored. Each
run saves `research_manifest.json` (collection IDs and SHA-256 hashes) and
`research.jsonl` (the exact released evidence per decision) beside its actions
and report. An updated article is a new captured version; it does not rewrite
past evidence. Normal simulator replay consumes recorded actions without Exa
or model calls; these logs allow the decision context to be inspected separately.

## Verification

```bash
python -m pytest -q meterless/tests simulator/tests
```

Tests use mocked Exa responses and the bundled market-data fixture. They cover
authentication and request bounds, invalid/missing dates, partial failures,
secret-safe errors, UTC day/hour filters, date-only precision, deduplication,
pinned snapshots, historical cutoff enforcement, all trader memory paths, and
simulator replay. A real Exa key is required to verify live provider connectivity.

Primary API reference: [Exa search](https://exa.ai/docs/reference/search).
