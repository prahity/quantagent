# TODO: current state

What is built, what is verified, and what is left, mapped to the four phases of `AI-Trader-Build-Guide.txt`. Last updated 2026-10-25.

## Done

### Phase 2: Independent simulator (complete)
- [x] Real data in `simulator/`: Yahoo hourly and daily bars, plus SEC EDGAR filings, for 8 symbols over 2024-01 → 2026-07.
- [x] Fixed JSON contract (`schema_version` 1.0).
- [x] V1 and V2 order modes, with fills at the next bar's open plus slippage. The engine applies the cash, inventory and 25% position-cap checks.
- [x] Report covering net profit, drawdown, Sharpe and turnover. Replaying a run's recorded actions reproduces its report.
- [x] News is gated by its timestamps, and asking for a future `as_of` returns HTTP 403.
- [x] FastAPI server (port 8711), Next.js UI (port 3177), and scripted test agents.
- [x] Agreed deviations from the guide:
  - one decision per hourly or daily bar, not per minute (no free minute data);
  - the simulator's own broker instead of HKUDS AI-Trader or Backtrader.

### Agent, first slice (repo root)
- [x] `agent/policy.py`: the Nemotron query → decision loop. Anything that fails becomes a hold.
- [x] `agent/model.py`: pinned model settings, used only when calling the Nemotron endpoint.
- [x] `agent/query_tool.py`: SQL limited to each decision's cutoff time, over the tables `prices` and `news`.
- [x] `agent/memory.py`: memory scoped to one run, returning only records available at the decision's cutoff.
- [x] `agent/contract.py`: local check of orders before they are sent.
- [x] `baselines/momentum.py`: deterministic baseline with no LLM.
- [x] `runner.py`: the only module that imports both the simulator and the agent.

### Phase 1: Zetaris connection (local Docker install)
- [x] **Query tool hardening:**
  - every table a query names must be plain `prices`, `news` or a CTE the query defines, checked by parsing with sqlglot;
  - results are limited to the run's symbols and lookback window (`FeedScope`), so both backends return the same rows.
- [x] **Zetaris backend:** JDBC (`jaydebeapi`), the hourly or daily price table chosen per run, a query timeout that cancels the statement, and Java values converted to plain Python.
- [x] **Runner `--backend zetaris`:** reads Zetaris instead of the per-run SQLite copy. It refuses to start unless Zetaris holds exactly the snapshot version being replayed.
- [x] **One-time publish of the whole snapshot** (`simulator/sim/publish.py`, `simulator/scripts/publish_zetaris.py`):
  - each row carries a hidden release time: bars at `bar_end`, news at `max(published_at, available_at)`;
  - verified to show the same rows as the simulator's own feed at every decision boundary.
- [x] **Setup and checks:** `simulator/config/zetaris_setup.sql` holds the statements that worked on this install, and `simulator/scripts/zetaris_probe.py` checks connection, permissions and dialect.
- [x] **Local instance configured** (Zetaris platform 2.4.3 in Docker; web UI on `localhost:3000`, JDBC on `localhost:10000`):
  - lake `aitrader_lake`, with tables `feed.feed_prices_hourly`, `feed.feed_prices_daily`, `feed.feed_news` and `feed.feed_manifest`;
  - simulator identity `admin@zetaris.com`, which publishes the data;
  - agent identity `aitrader-agent@zetaris.local`, with role `aitrader_agent_role` and SELECT on the feed only. Its writes and deletes are refused.
  - The fixture slice is published: AAPL, MSFT and SPY, 2025-01-27 → 2025-02-07, with 210 hourly bars, 30 daily bars and 5 news items.
- [x] **Config:** `.env.example`, the dependencies (`sqlglot`, `jaydebeapi`, `jpype1`), a JDK 17 requirement, and `CLAUDE.md`.

- [x] **Zetaris is the default backend locally** (`QUERY_BACKEND=zetaris` in `.env`). Use `--backend sqlite` to run offline. The offline tests pin SQLite.

### Verified
- [x] `python -m unittest discover tests`: 43 tests pass (3 live tests skipped by default).
- [x] `cd simulator && .venv/bin/pytest -q`: 33 tests pass.
- [x] `scripts/zetaris_probe.py`: every check passes for both identities.
- [x] `ZETARIS_LIVE=1 python -m unittest tests.test_zetaris_live`: 3/3 pass. A momentum run gives identical orders, profit and query-result hashes on Zetaris and SQLite.

## To do

### Next up
- [ ] **Commit the Zetaris work.** First add `.history/` to `.gitignore`; it is an editor history folder that may contain copies of `.env`.
- [ ] **Load the full dataset into Zetaris.** Until then, `--backend zetaris` only works with `--data fixtures`.
  ```bash
  cd simulator
  SEC_USER_AGENT="Name email" ../.venv/bin/python scripts/fetch_data.py
  ../.venv/bin/python scripts/publish_zetaris.py --replace
  ```
- [ ] **Run Nemotron on Zetaris:**
  - fill `NEMOTRON_BASE_URL`, `NEMOTRON_API_KEY` and `NEMOTRON_MODEL_ID` in `.env`;
  - tell the policy prompt (`agent/policy.py`) that the backend speaks Spark SQL. SQLite-only functions like `strftime` fail on Zetaris.
- [ ] **First real Nemotron run:** `python runner.py --agent nemotron --backend zetaris ...`. No run against the live endpoint has been done yet.

### Phase 1: remaining
- [ ] Agent workspace tables in Zetaris (`features`, `findings`, `predictions`, `strategies`, `query_calls`, `decision_nodes`, `decision_edges`, …). The query tool would need a narrow, guarded INSERT path for these; today the agent can only read.
- [ ] Store `evidence_query_ids` with each decision. The policy asks for them but does not persist them.
- [ ] Data products: `decision_context` first. Then `strategy_performance_by_context`, `strategy_lineage_evidence` and `evaluation_report`, which need results from many runs.
- [ ] Fuller news:
  - article bodies (SEC titles are item names only);
  - content versions and tracking of later edits;
  - Alpha Vantage or Finnhub sources;
  - derived features with their own timestamps.
- [ ] Relevance policy: an agent-declared watchlist, sector and market-wide news, and fixing the digest rules in the run config.
- [ ] Data validation: a check for price gaps, and a written note on how splits and dividends are treated.
- [ ] Optional: a cap on each order's share of bar volume, and slippage that grows with order size. Fills currently ignore volume.

### Phase 2: open items
- [ ] Memory-reset test: a marker left in run A must not be visible in run B after restoring a checkpoint. Today memory is simply fresh for each run.
- [ ] Concurrency: a per-run lock in `SimulatorService`, if several agents will hit one server at once.

### Phase 3: Meterless (not started)
- [ ] Strategy records with version and parent IDs, and the predeclared windows and budgets.
- [ ] Planner that sets several strategy agents on different hypotheses (momentum, mean reversion, news-confirmed).
- [ ] Run each candidate in its own simulator account with capital K under matched conditions, with a coordinator for concurrent runs.
- [ ] Graph linking rules → queries → decisions → fills → outcomes → revised rules.
- [ ] Bounded retrieval of eligible past experience at each decision.
- [ ] Compare, combine and critique candidates, and re-run every combination against its parents.
- [ ] Immutable checkpoints: generation 0, intermediate generations, and final.

### Phase 4: Sealed evaluation (not started)
- [ ] Full reset cycle: restore the checkpoint, use fresh runtime memory, and discard what was learned during the test.
- [ ] Sealed test window from `simulator/config/splits.yaml` (2026-07-01 → 2026-07-21).
- [ ] Compare base, intermediate and final checkpoints with buy-and-hold and the momentum baseline.
- [ ] Ablations: without Zetaris, without Meterless, memory off, no news.
- [ ] Query-planning benchmark, and the more-data-versus-better-querying experiment.
- [ ] Learning charts (net profit by generation) and the exported evaluator records.
- [ ] Live paper-trading mode with an evaluator-side research agent (optional).
- [ ] Track 03 demo.

## Quick reference
```bash
python -m unittest discover tests                                    # agent tests
cd simulator && .venv/bin/pytest -q                                  # simulator tests
cd simulator && ../.venv/bin/python scripts/zetaris_probe.py         # Zetaris health check
python runner.py --agent momentum --data fixtures --backend zetaris  # run on Zetaris
```
Zetaris settings live in `.env` (template: `.env.example`). Setup details: `simulator/README.md`, section "Publishing the released feed to Zetaris".
