# Build guide

Oct 21, 2026 · @Sai Ramsankar

Build in five stages, in order. Each depends on the one before and ends with a check you can run.

&#91;embedded content: build roadmap · 5 stages, 5 exit checks\]

Don't start the agent until the simulator passes its leak tests. An agent tuned on a leaky simulator learns to exploit the leak, not the market.

## Step 0: What to download

Install these before writing any code. Get one LLM API key and use it in both Swarms and your own loop.

| Item | What it's for | How to get it |
| --- | --- | --- |
| Python 3.11+ | Simulator, data loaders, the iteration loop | python.org, or the `uv` installer |
| Git | Version control; cloning Swarms | git-scm.com |
| Meterless Swarms (desktop) | Designing and inspecting the agent graph | `.msi` or `.exe` from the [Swarms GitHub](https://github.com/Meterless/Swarms) releases page. Windows only today; Mac and Linux installers are planned |
| Node.js + npm | Running Swarms from source on Mac or Linux | nodejs.org, then `npm install` and `npm run dev` in the cloned repo |
| Zetaris instance | The agent's source for analyzing prices and news | Zetaris Cloud or Enterprise. You need host, port (default 10000), username and password |
| Zetaris JDBC driver | Lets the agent's query tool reach Zetaris | A single `.jar`, in `ZETARIS_DIR/jdbc-driver/` or downloadable from the [Zetaris knowledge base](https://kbase.zetaris.com/knowledge/dataiku-to-zetaris) |
| Java JDK | Runs the JDBC driver from Python | Any current OpenJDK build |
| LLM API key | Every agent call | Anthropic, OpenAI, Google or OpenRouter; Swarms supports all four |
| Price data | Historical OHLCV bars | `yfinance` to prototype; a paid vendor once results matter |
| News data | Articles with original publish times | GDELT (free, timestamped) to start |

Python packages, installed in a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install pandas numpy pyarrow pydantic python-dotenv \
            jaydebeapi jpype1 yfinance requests trafilatura \
            anthropic matplotlib pytest
```

Put secrets in a `.env` file that git ignores: `ZETARIS_HOST`, `ZETARIS_PORT`, `ZETARIS_USER`, `ZETARIS_PASSWORD`, `ZETARIS_JDBC_JAR`, `ANTHROPIC_API_KEY`.

One open question: Swarms documents no API, CLI or headless mode, and stores its runs in the app's own browser storage. Plan for your Python code to drive the loop; use Swarms to prototype and inspect the agent roles.

## Repo layout

The simulator and the agent are completely separate. The agent analyzes data through Zetaris; the simulator runs on its own local snapshot. They share only the strategy spec and the score report.

```text
ai-trader/
├── .env                  # secrets, git-ignored
├── config/
│   └── splits.yaml       # train / validation / test date ranges
├── data/
│   ├── zetaris_client.py # JDBC connection + query helpers
│   ├── load_prices.py    # pull OHLCV, write to Zetaris
│   ├── load_news.py      # pull dated articles, write to Zetaris
│   └── export_snapshot.py# write the simulator's Parquet snapshot
├── sim/                  # SIMULATOR: never imports agent/ or Zetaris
│   ├── snapshot/         # frozen Parquet files
│   ├── engine.py         # day-by-day backtest loop
│   ├── broker.py         # fills, commissions, slippage
│   ├── feed.py           # as-of access to the snapshot
│   └── metrics.py        # Sharpe, drawdown, P&L
├── strategy/             # SHARED CONTRACT between the two sides
│   ├── spec.py           # pydantic schema the agent fills
│   └── report.py         # schema of the score report
├── agent/                # AGENT: never imports sim/
│   ├── tools.py          # Zetaris query tool, capped by date
│   ├── roles.py          # analyst, proposer, critic prompts
│   ├── loop.py           # the iteration driver
│   └── memory.py         # past strategies + scores
├── runs/                 # one folder per iteration
└── tests/
```

## Phase 1: Data layer

Goal: prices and dated news in Zetaris, queryable from Python by symbol and date.

1. **Connect.** Write `zetaris_client.py` using JayDeBeApi with driver class `com.zetaris.lightning.jdbc.LightningDriver` and URL `jdbc:zetaris:lightning@<host>:<port>` ([Zetaris KB](https://kbase.zetaris.com/knowledge/zeppelin-to-zetaris)). Confirm with `SELECT 1`. Zetaris also offers ODBC and a REST API if JDBC is awkward ([overview](https://kbase.zetaris.com/knowledge/overview-of-connecting-to-zetaris)).
2. **Pick a universe.** Start small: 10 to 20 liquid tickers, daily bars, 5 to 10 years.
3. **Load prices.** Table `prices(symbol, date, open, high, low, close, volume, adj_close)`. Use split- and dividend-adjusted prices.
4. **Load news.** Table `news(id, symbol, published_at, source, title, body, url)`. Keep the original `published_at`; never the time you scraped it.
5. **Validate.** Check for missing days, duplicate rows and any article with no timestamp. Drop untimestamped articles rather than guessing.
6. **Freeze a snapshot.** Version the dataset so iteration 50 is scored on the same data as iteration 1. Export prices and sentiment scores to Parquet in \`sim/snapshot/\`; the simulator never connects to Zetaris.

Done when: one query returns all bars and all news for a symbol up to a given timestamp.

## Phase 2: Simulator

Goal: a deterministic backtest that scores any strategy spec on any date range.

&#91;embedded content: one simulated trading day · 4 steps, 1 blocked path\]

1. **Build `feed.py` first.** It reads only the local snapshot, never Zetaris or the agent: `get_bars(symbol, as_of)` and `get_sentiment(symbol, before)`, each filtered by date in the query itself.
2. **Define `strategy/spec.py`.** A pydantic schema the agent must fill: universe, entry and exit rules, indicator parameters, position size, stop loss, news-sentiment weight. The agent never writes free-form code.
3. **Precompute news sentiment.** Score each article once with an LLM, store the score in Zetaris for the agent, and copy it into the simulator's snapshot. The strategy reads scores through the feed, so there is no LLM call per simulated day.
4. **Write `broker.py`.** Orders made after day t's close fill at day t+1's open. Charge a commission per trade and slippage in basis points. Long-only to start.
5. **Write `engine.py`.** The day loop in the diagram above.
6. **Write `metrics.py`.** Total return, annualized Sharpe ratio, max drawdown, trade count, turnover.
7. **Test it.** A leak test (the feed raises an error if asked for data after `as_of`), a determinism test (the same spec twice gives identical metrics), and a tiny hand-computed example.

Done when: a buy-and-hold spec matches the plain buy-and-hold return minus costs, and the leak tests pass.

## Phase 3: Agent loop

Goal: an unattended loop where the agent proposes a spec, the simulator scores it across walk-forward folds, and memory carries the history forward.

&#91;embedded content: walk-forward split · 4 folds and a locked test\]

1. **Prototype the roles in Swarms.** Describe the four roles (market analyst, news analyst, strategy proposer, critic) and paste a sample results summary. Inspect the graph it builds and the specs it proposes. Copy the prompts that work into `agent/roles.py`.
2. **Give the analysts a Zetaris query tool.** Write `agent/tools.py`: read-only SQL over the prices and news tables that adds a date cutoff to every query, set to the end of the current fold's training window. Back it with a Zetaris view or access policy so the cap can't be bypassed.
3. **Write `agent/memory.py`.** Append each iteration's spec, per-fold scores and the critic's notes to a JSON-lines file in `runs/`. Swarms keeps its memory inside the app, so your loop needs its own.
4. **Write `agent/loop.py`.** Each iteration:
   1. Load the top past specs and their scores from memory.
   2. The analysts query Zetaris and summarize what worked and what failed.
   3. The proposer returns a new spec as JSON, validated against the pydantic schema.
   4. The critic rejects near-duplicates and specs that break risk limits.
   5. The loop hands the spec to the simulator, which scores it on every fold and returns only a score report. Score = mean validation Sharpe minus a drawdown penalty.
   6. Save everything, then repeat until a fixed budget or no improvement for several iterations.
5. **Keep the boundary one-way.** Only two things cross between the sides: the strategy spec (agent to simulator) and the score report (simulator to agent). Run the simulator as its own process or service so the agent cannot import it.
6. **Log token cost per iteration** so a long run doesn't surprise you.

Done when: 20 iterations run unattended, and the best score can be reproduced from its saved spec.

## Phase 4: Evaluation and hardening

Goal: know whether the agent learned anything real before trusting any number it produces.

1. **Open the test set once.** Score the best validation strategy on the locked test period. If it collapses there, the agent overfit.
2. **Compare against baselines.** Buy-and-hold and a simple moving-average rule. The agent must beat both after costs.
3. **Run a no-news ablation.** Score the same strategies with news switched off, to see whether the scraping adds anything.
4. **Test after the model's cutoff.** Run on a period the LLM cannot have seen, to rule out memorized history.
5. **Stress costs.** Double slippage and commissions; a real edge should survive.
6. **Paper trade.** Only then run it live against a paper account for a few months.

Done when: the strategy beats baselines on the test period, after costs, with and without its best-case assumptions.

## Sources

- [Meterless Swarms (GitHub README)](https://github.com/Meterless/Swarms): installers, local dev steps, supported providers
- [Zetaris: overview of connecting](https://kbase.zetaris.com/knowledge/overview-of-connecting-to-zetaris): JDBC, ODBC and REST access
- [Zetaris: connect Zeppelin](https://kbase.zetaris.com/knowledge/zeppelin-to-zetaris): JDBC driver class, URL format, default port
- [Zetaris: connect Dataiku](https://kbase.zetaris.com/knowledge/dataiku-to-zetaris): where the JDBC driver jar lives
