# Meterless Runtime

AI Trader calls Nightshift for Nemotron. The NVIDIA API key is entered in Nightshift's Providers tab. This package never reads it.

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

`NemotronTrader` sends one simulator decision request to that route and expects the fixed action JSON back. A reply that is not that JSON is a hold.
