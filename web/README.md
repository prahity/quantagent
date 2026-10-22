# Simulator web interface

Next.js (App Router, TypeScript, Tailwind, shadcn/ui, recharts) front end for the independent simulator in this repository. It talks only to the FastAPI runner layer; it never touches the snapshot or the engine directly.

```bash
# in the repo root, start the API first
.venv/bin/uvicorn server.app:app --port 8711 --reload

# then
cd web
npm install
npm run dev -- --port 3177
```

Open http://127.0.0.1:3177. Requests to `/api/*` are rewritten to the API (`SIM_API_URL`, default `http://127.0.0.1:8711`).

Pages:

- `/` — runs table, snapshot card, new-run form (hourly/daily, V1/V2, dates, symbols, K, costs).
- `/runs/<run_id>` — KPIs, released-feed price chart with fills and a "read one week ahead" probe (expected to be refused), equity and drawdown, news digest, manual order entry, test-agent autoplay, fills, rejections, decision log, report and replay check.
- `/data` — universe, chronological splits, snapshot coverage, news sources.

Checks: `npx tsc --noEmit` and `npm run lint`.
