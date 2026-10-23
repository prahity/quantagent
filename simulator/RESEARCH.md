# Research in the simulator loop

Each decision now gets a sourced brief before it reaches the caller. The trading
action contract and execution engine still determine all fills and scores.
Research cannot submit orders or supply execution prices.

| Path | Researchers | Evidence | External calls |
| --- | --- | --- | --- |
| Historical simulation | Deterministic catalyst, market, and risk specialists | Released filings and completed price bars; optionally a pinned eligible collection | None |
| Live research collection | Company-news, filings/risk, and macro searches; optional Fireworks synthesis | Exa source text with URLs and timestamps | At most five Exa searches and one Fireworks completion |

Historical specialists are extractive/rule-based, not LLM agents. Exa retrieves
current source text; Fireworks optionally interprets the evidence. This is an
evaluator-side context layer. Live prices/paper brokerage, the Nemotron trading
policy, Zetaris registration, and Meterless coordination remain separate work.

## Offline demo and checks

From `simulator/`, with the Python project dependencies installed:

```bash
python scripts/demo_research.py
python -m unittest tests.test_research tests.test_research_api -v
pytest
```

The demo uses the committed real January 2025 AAPL/MSFT data, prints a brief,
advances a HOLD-only run, and checks accounting/research replay. It makes no
network calls and does not claim strategy performance. `pytest` includes the
existing regression suite. The five new HTTP tests explicitly skip if FastAPI
is unavailable; the other research tests require only existing core dependencies.

## Per-decision API

Research is enabled by default. Configure a run with:

```json
{
  "start": "2025-01-28",
  "end": "2025-01-31",
  "symbols": ["AAPL", "MSFT"],
  "research": {"enabled": true, "max_news": 12, "price_bars": 20}
}
```

The additive, nullable `data.research_brief_id` references a `research-1.0` brief.
Clients validating requests with an older strict schema must update `DataRefs`.
The existing action JSON is unchanged.

`GET /api/runs/{run_id}/research` returns `{"brief": ...}` for the current
decision (last decision after completion). Use `?request_id=decision_00000` for
an earlier emitted brief. Future/unissued requests return 404. Disabled research
returns `{"brief": null}`. Local and HTTP readers expose `feed.research(request_id)`.

Briefs include sources, findings with `source_ids`, gaps, the exact `as_of`, and
a content-derived ID. Market statistics describe observed close-to-close returns
and close-based drawdown over the stated completed-bar window, not forecasts.
Use `research.enabled=false` or `runner.py run --no-research ...` for ablations.
The existing scripted trading policies are unchanged; a policy must explicitly
consume the brief before it can affect decisions. No P&L improvement is claimed.

## Live API setup

The adapters use the existing `httpx` dependency, without vendor SDKs. Set these
variables in the API server's process environment; `.env.example` is a template
and is not loaded automatically:

| Variable | Purpose |
| --- | --- |
| `EXA_API_KEY` | Exa search credential |
| `FIREWORKS_API_KEY` | Optional inference credential |
| `FIREWORKS_MODEL` | Pin a JSON-schema-capable model/deployment available to your account |
| `RESEARCH_API_TOKEN` | Operator bearer token for collection/archive endpoints |

Keep keys on the server, never in browser code or `NEXT_PUBLIC_*`. No real keys
are included; integration tests use mocked provider responses.

```bash
curl http://127.0.0.1:8711/api/research/providers

curl -X POST http://127.0.0.1:8711/api/research/collections \
  -H "Authorization: Bearer $RESEARCH_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"symbols":["AAPL","MSFT"],"lookback_days":7,"max_results":3,"synthesize":true}'
```

The response contains a `collection_id`, `available_at`, sources, findings,
warnings, status, and the synthesis model when used. Check `status` and `warnings`:
empty/partial results are explicit. Synthesis defaults to false. Missing Fireworks
settings or a failed completion retain the raw evidence with a warning.

`GET /api/research/collections/{collection_id}` reads the immutable saved result
and requires the same bearer token. Provider status exposes configuration booleans,
never credentials. Collection works without a loaded price snapshot. Symbols must
belong to `config/universe.yaml`; arbitrary queries and historical cutoffs are rejected.

## Time safety and replay

### Organize, sort, and filter every retrieved record by day and hour

Exa requests explicitly prioritize original publication timestamps for UTC day/hour
organization. The application derives and saves `published_day` and
`published_hour` on every evidence record, plus equivalent `available_*`,
`released_*`, and `retrieved_*` buckets. For example, a publication timestamp of
`2026-10-22T14:35:12Z` produces day `2026-10-22` and hour
`2026-10-22T14:00:00Z`. All timestamps and buckets use UTC. Source timestamps are
preserved at full precision; buckets never replace eligibility checks. Records
with no retrieval timestamp have null retrieval buckets. Incorrect caller-supplied
buckets are rejected. Older archive records acquire these fields when loaded.

Three GET endpoints expose the same organization and query options:

| Endpoint | Scope |
| --- | --- |
| `/api/research/sources` | Every saved Exa collection; requires operator bearer token |
| `/api/research/collections/{collection_id}/sources` | One saved collection; requires operator bearer token |
| `/api/runs/{run_id}/research/sources` | Already-emitted research evidence, with optional `request_id`; simulator cutoff still enforced |

Supported parameters:

| Parameter | Meaning |
| --- | --- |
| `day=2026-10-22&hour=14` | Exact UTC day and hour (hour is 0–23); either filter may be used alone |
| `start=...&end=...` | Time-zone-aware timestamp range; start inclusive, end exclusive |
| `time_field=published_at` | Filter and group by `published_at`, `available_at`, `released_at`, or `retrieved_at` |
| `symbol=AAPL&provider=exa&kind=web` | Filter records by company, provider, or evidence kind |
| `text=earnings` | Case-insensitive search of title/content |
| `group_by=hour` | Return hour or day buckets, including counts across all filtered results |
| `sort_by=published_at&sort_order=desc` | Ascending/descending timestamp, symbol, or provider ordering; stable ties and nulls last |
| `offset=0&limit=100` | Paginate records; limit is capped at 500 |

```bash
curl -G http://127.0.0.1:8711/api/research/sources \
  -H "Authorization: Bearer $RESEARCH_API_TOKEN" \
  --data-urlencode 'day=2026-10-22' \
  --data-urlencode 'hour=14' \
  --data-urlencode 'symbol=AAPL' \
  --data-urlencode 'time_field=published_at' \
  --data-urlencode 'group_by=hour' \
  --data-urlencode 'sort_by=published_at' \
  --data-urlencode 'sort_order=asc'
```

The response includes `timezone`, `time_field`, `buckets`, `total`, and paginated
`records`. Archive-wide results retain `collection_id`, including repeated source
versions collected in separate batches. Buckets contain observed records only;
empty hours do not imply a completed search of that hour. Exa search results are
bounded samples, not exhaustive coverage of all market news. Date-only/undated
provider results are excluded rather than assigned an invented publication hour.
Archive-wide queries scan saved JSON collections; a database index can be added
if collection volume outgrows this hackathon-scale implementation.

### Keep publication and availability separate

Publication-date filters do not prove that today's page content existed in the
past. Undated, invalid, future, and out-of-window search results are excluded.
Retrieved versions are hashed and stamped with actual retrieval times. Derived
findings become available only after the entire collection/synthesis finishes.

Attach the returned ID using `research.collection_id` on a new run (or
`runner.py run --research-collection ID ...`). Every run symbol must be covered.
Comparison runs can use the same ID. The service pins a full copy for each run;
new searches never mutate an existing run. Content stays unavailable until its
original availability time is reached, including synthesis latency.

**A collection fetched today cannot enrich January 2025 fixtures or the current
snapshot ending in July 2026.** It needs newer price data for evaluation after
its availability date. No fabricated historical research or price data is added.

Each run saves `research.jsonl`, plus `research_collection.json` if attached.
Replay rebuilds briefs offline and returns `account_match`, `research_match`, and
combined `match`. Missing/changed research logs fail the combined result. Older
runs without research logs retain their accounting replay behavior. Existing run
IDs cannot overwrite saved artifacts.

The raw collection archive is evaluator-owned and gitignored. A run's research
feed only exposes eligible evidence. The future Zetaris adapter should publish
that released evidence, not the raw archive. The current feed is for simulator
and test-harness integration.

## Provider decisions and limits

Exa Search gives this implementation direct control over query count and timing;
its hosted Agent API is not implemented. Two searches per company plus a macro
search cover up to two symbols, with at most five results per query. The collection
has a 55-second overall timeout and one concurrent collection per API process.
There are no automatic retry charges. This is a single-process hackathon service,
not a distributed queue or global rate limiter.

Fireworks receives untrusted evidence as data and no browsing/execution tools.
Schema validation rejects incomplete or malformed output, unknown symbols and
unknown citations. All accepted model prose is labeled as interpretation.
Citation checks verify source references, not semantic truth; source text remains
available for review. The environment owns trading results independently.

Official API references checked for this implementation:

- [Exa Search](https://exa.ai/docs/reference/search)
- [Exa content limits](https://exa.ai/docs/contents/quickstart)
- [Fireworks chat completions](https://docs.fireworks.ai/api-reference/post-chatcompletions)
- [Fireworks structured outputs](https://docs.fireworks.ai/structured-responses/structured-response-formatting)
