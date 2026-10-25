# Session handoff

This file is the context from the cloud Cursor chat. A new agent on the Mac does not see that chat. Read this file, then `simulator/context/work.md`, `meterless/README.md`, and `README.md`.

## Where things run

Nightshift is installed on the Mac and is already listening:

- `127.0.0.1:8088` and `[::1]:8088`
- LAN `10.2.120.26:8088`
- Process name `nightshif`

`GET http://127.0.0.1:8088/v1/models` with the Console API key and header `meterless-app: ai-trader` returned `meterless/auto`, `meterless/fast`, `meterless/deep`, `meterless/cheap`, `meterless/local`, `meterless/fusion`, plus NVIDIA Nemotron cloud lanes (`nvidia/nemotron-3-super-120b-a12b`, `nvidia/nemotron-3-ultra-550b-a55b`, `nvidia/nemotron-3.5-lightning-30b-a3b`, and the Llama Nemotron lanes) and small on-device `webllm/*` models.

The cloud Cursor machine cannot open that port. `curl` to `127.0.0.1:8088` from the cloud workspace fails. Every Meterless call has to run in a terminal on this Mac, with Nightshift open.

The NVIDIA API key belongs only in Nightshift, Setup, Providers, NVIDIA. It must not be written into `.env`, code, or git. The app's secret is the Nightshift Console runtime token, stored only in the gitignored `.env`:

```bash
METERLESS_BASE_URL=http://127.0.0.1:8088/v1
METERLESS_RUNTIME_TOKEN=<Nightshift Console API key>
METERLESS_MODEL=meterless/auto
```

Do not print the token. `python -m meterless` reports online, unauthorized, or offline and never echoes it.

Local checkout used for this work:

`/Users/prahityaugand/Desktop/nonScreenShots/code/current/quantagent`

Git remote: `https://github.com/prahity/quantagent.git` on `main`.

## What is already built

`simulator/` is the market environment. It replays real Yahoo hourly and daily bars and SEC EDGAR filings from 2024-01-02 through 2026-07-21 for AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, and SPY. No synthetic prices. `sim/` never imports `agent/` or `meterless/`. The scripted agents under `simulator/agent/` are deterministic test harnesses, not the trading model.

`meterless/` is the OpenAI-compatible client for Nightshift (provider contract v4, Runtime 1.4). `python -m meterless` probes `GET /v1/models`. `NemotronTrader` in `meterless/trader.py` sends one simulator decision request and expects `schema_version` `1.0` action JSON. A reply that is not that JSON becomes a hold. The model does not set the fill price.

Chat completions are not confirmed yet. The model list succeeded. The next check, from this Mac checkout, is:

```bash
source .venv/bin/activate
set -a && source .env && set +a
python -m meterless --say "Reply with one sentence confirming this connection works."
```

Run that from the repository root so `meterless` imports. `httpx` has to be installed in that venv. After a fresh clone, the price snapshot is absent because `simulator/sim/snapshot/` is gitignored. Rebuild it with `python scripts/fetch_data.py` from `simulator/` before a real run. Expected manifest id: `snap-17f602c41277a742`.

## Next task

Ignore Zetaris for now. Compare Nemotron with Meterless memory against Nemotron without it.

Both arms use the same Nightshift route, the same model lane, the same window, the same symbols, and the same starting capital. Suggested first window: daily bars, January 2025, symbols AAPL, MSFT, SPY, cash 10000, mode V1. Pin one Nemotron id for both arms rather than leaving routing to chance. `meterless/auto` may pick a different lane per call. A pinned id such as `nvidia/nemotron-3-super-120b-a12b` keeps the model fixed. Nightshift still has to be the endpoint, so the Power rule (Meterless runtime in front of Nemotron) stays intact.

- Without memory: each decision is a fresh prompt. Send only the current simulator decision request. No prior orders, fills, or lessons.
- With memory: keep a bounded log of this run's earlier decisions and the simulator's acknowledgements, and include that log in the prompt. This is in-run memory, not a claim that Nightshift's own memory product was evaluated.

`simulator/runner.py` only loads `hold`, `buy_and_hold`, and `script`. Wiring `NemotronTrader` through `run_agent` is still to do. Record both reports (`net_profit`, `return_pct`, `max_drawdown_pct`, `fills`, `rejections`, `invalid_responses`) and the model id. A hold-only run means the model missed the JSON contract. Fix the parse or the prompt before treating the score as a trading result.

One decision per completed bar. January 2025 daily on three symbols is on the order of twenty model calls per arm. Do not start with the full 2024–2026 hourly history.
