# AI Trader — Learning to query a growing Zetaris knowledge base

**Sai Ramsankar & Prahit Yaugand** · Open Agent Hackathon 2026 · Track 03: Reasoning Architecture

An adaptive trading-agent research system in which **Zetaris builds the market intelligence**, **Meterless coordinates strategy experiments and memory**, **Nemotron reasons and produces actions**, and an **independent simulator/evaluator measures results**.

The project investigates whether a fixed Nemotron model can make better decisions as Zetaris accumulates eligible market history and Meterless remembers how earlier analyses and decisions worked out. The learned behavior includes what to investigate, which SQL queries to make, and how to turn returned evidence into actions. Its target is the best validated strategy found within a declared budget, with traceable decisions and controlled information access.

**Status:** the simulator is in [`simulator/`](simulator/README.md) and runs on real 2024–2026 market data. The Meterless client, the Nemotron trader, and the SQL adapter are in [`meterless/`](meterless/README.md). Setup for a new machine, including every file git leaves out, is [`context/session-handoff.md`](context/session-handoff.md). The sections below are the research design. Zetaris views are selected with `ANALYSIS_BACKEND=zetaris`; the default backend is the local sqlite snapshot.

## Data — Zetaris

Zetaris is the full system's primary market-intelligence layer. Trading decisions and strategy revisions use evidence built by joining complementary registered sources through it. The [official platform overview](https://www.zetaris.com/cloud) describes federated access and a semantic layer; our financial views and evaluation protocol are application components to build.

### Sources and their roles

| Source | Example underlying representation | Contribution |
| --- | --- | --- |
| Prices and market activity | Versioned files/object storage or a market-data table | Returns, trends, volatility and available prices |
| News and filings | Timestamped article/event records with versioned text | Company announcements and market context |
| Financial/company events | Structured historical tables | Earnings, company identifiers, sectors and event classifications |
| Simulator/evaluator results | Evaluator-owned order, fill and account ledger | What each strategy actually did and how it performed |

These are planned source categories, not a claim that connectors or datasets are already available. Keep distinct sources independently registered where practical. Parse and timestamp outside content on the environment side before exposing it to agents. Pin source versions for historical replay.

### Data products that drive the agent

| Project-defined product | Joins | Used for |
| --- | --- | --- |
| `decision_context` | Released prices, news, company events and permitted portfolio state | Evidence for the current trading decision |
| `strategy_performance_by_context` | Completed development trials, entry-time features, sector/event metadata | Choosing which strategy or modification to investigate next |
| `strategy_lineage_evidence` | Strategy versions, parent links, SQL analyses and available outcomes | Grounding remembered lessons and proposed combinations |
| `evaluation_report` | Official account/fill records, checkpoint IDs and environment metadata | Reliable comparisons and plots |

For example, the system can investigate: **How did momentum strategies behave around earnings events across different sectors and volatility levels?** Zetaris joins the evidence from those sources. Meterless uses the returned results to propose a revised earnings filter or sizing rule. Nemotron implements that proposal as a candidate policy, and the simulator evaluates it. A merged strategy is tested again before it is treated as an improvement.

This makes Zetaris part of both the trading loop and the learning loop. Every substantive action or revision references its query IDs, source records and data cutoff. Shared definitions cover symbols/company IDs, time zones, currency, feature windows and result units. Complete analytical and decision-history records persist through a configured writable source/lakehouse; Meterless retrieves eligible summaries and references through our adapter.

### A growing knowledge base and a learned query policy

Retain all released historical news, versioned article text and financial/price records in the registered stores exposed through Zetaris. Preserve raw records alongside derived event labels, features and analyses, with source and availability timestamps. The knowledge base grows as the environment releases information; an old article remains available for later SQL analysis.

Treat `zetaris_query` as a separate, instrumented project tool. Nemotron receives an analysis task and schema, then produces an analysis goal and SQL. The adapter enforces the environment's cutoff and permissions and returns rows, provenance and a query ID. SQL is distinct from the fixed JSON trading-action interface; Zetaris is the database/query service, not another language model.

Meterless retrieves relevant decision-to-outcome experience and analytical rules: which sources to join, which time windows to inspect, what failed, and when more evidence is needed. For example, a proposed rule might request sector-relative returns and archived earnings news before interpreting a price jump. It remains a hypothesis until checked on later validation windows. Correlation does not establish that news caused a return.

The [Co-LMLM paper](https://arxiv.org/abs/2607.07707) motivates externalizing knowledge, but its trained continuous retrieval differs from this explicit SQL-tool design. We do not reproduce its method or assume its results transfer to trading.

### Time-scoped access

The simulator or live research service determines when each record is available. The project then exposes only the relevant released tables/views and enforces access to them. An agent cannot obtain future information by omitting a SQL date filter or choosing another connector.

For a decision at time `t`, publication, source availability, environment release and any derived result must be eligible by `t`. Apply this to joined statistics, memory summaries and graph edges too. Keep private archives, source administration and official evaluator writes inaccessible to trading agents.

The simulator may execute its private replay independently. In the full agent, the analytical path uses Zetaris rather than a parallel CSV/vendor shortcut. The without-Zetaris ablation explicitly rebuilds equivalent data products using the declared replacement backend.

## Meterless requirements

Meterless coordinates a complete strategy-improvement round:

1. Combine joined Zetaris evidence with eligible previous strategies, results and lessons.
2. Plan work for agents investigating distinct hypotheses, such as momentum, mean reversion and news-confirmed trading.
3. Submit each versioned candidate to an isolated simulator account with starting capital `K` and matched evaluation conditions.
4. Remember successful and failed trials, including their decisions, actions, executions, measured outcomes and uncertainty.
5. Propose new strategy versions, combinations or condition-based selectors using that evidence.
6. Rerun proposals, compare them with their parents, and retain validated improvements within the trial/token budget.

Maintain a directed acyclic history linking prior rules, analysis questions, query attempts/results, decisions, predictions, actions, measured outcomes and revised rules, alongside strategy-version lineage. Preserve successful and failed attempts. Each rule records its scope, supporting cases, counterexamples, confidence and version; one profitable trade does not establish a rule. Keep complete records durable and retrieve a bounded relevant context for each prompt.

Planning reads Zetaris. A joined result over released prices, filings, and completed trials is what selects the next hypothesis. Meterless retrieves the prior decision, outcome, and analytical rule that say which join to run. Nemotron writes that SQL and the candidate policy. The simulator scores the candidate. A parallel writing swarm does not see the cutoff or the ledger, so it is not part of this loop. The trading-history schema, feedback capture, and Zetaris synchronization are project integrations to verify.

## Nemotron

Use NVIDIA Nemotron for trading agents and the planning, critique, synthesis and reflection stages. Pin the actual model ID/version, endpoint and inference settings. Keep that model fixed across the base agent, intermediate checkpoints, final agent and tool ablations.

Learning initially means evolving strategy policies/prompts and eligible memory. Model-weight fine-tuning is a separate method if explicitly added. Validate generated actions against the fixed JSON contract; a model response does not determine its own execution price or official score.

Open source does not imply weak reasoning. Select a model that fits the available resources and measure its ability to plan queries, generate SQL and interpret uncertainty. External knowledge and memory can supply evidence, but their benefit remains an experimental question.

## Independent simulator and evaluator

The environment owns the clock, market/context release, fills, costs, positions and official performance report. Trading agents have no web-search or general browsing tools.

| Mode | Context source | Timing |
| --- | --- | --- |
| Historical simulation | Frozen prices, news and financial context released by the simulator | Known simulated clock and configurable playback acceleration |
| Live-market paper evaluation | Live data plus an evaluator-side research agent | Actual market time; inference latency counts |

The live research agent can search for news, filings, earnings and relevant context. It provides a common sourced, timestamped feed to comparison agents. Historical evaluation uses archived as-of information. A modern search result cannot be relabeled as information known in the past.

**V1:** one trading-decision request per simulated minute, returning immediate orders or hold as fixed JSON. **V2:** one request produces a plan for the next simulated minute, with agent-chosen activation times. The simulator activates orders and determines fills as eligible market events arrive. Intraminute scheduling requires sufficiently fine market data.

Capital `K` and playback speed are separate configuration values. Live evaluation uses a time scale of 1. The companion architecture document defines the request and action examples. A suitable pinned HKUDS AI-Trader integration or another inspected execution engine can be used after verifying the required accounting, context and timing controls.

## Chronological learning and test-memory reset

1. Develop on recent historical years in chronological order, learning from earlier windows and validating on later ones.
2. Reserve the latest complete interval as a sealed testbed before development begins.
3. Save immutable base, selected intermediate and final checkpoints containing policy, prompts, eligible training memory, model settings and source cutoffs.
4. Before each test/repeat, restore that candidate's own pre-test checkpoint, reset the environment to capital `K`, and allocate fresh run IDs, account state, SQL namespace, conversation/cache state and writable memory.
5. Allow the agent to store and use new information as that test run advances. Only outcomes already observed in the run can affect a later decision.
6. After the test, export evaluator-only audit records, discard all test-derived runtime learning, and restore the pre-test state for any preregistered repeat. Test memory must not enter another candidate, a replay or further development.

Preselect the checkpoint comparison set before opening the sealed test. Use validation feedback to develop strategies; keep sealed test results outside the improvement loop. Chronological data controls cannot erase historical knowledge already present in a pretrained model, so forward paper evaluation provides additional evidence.

## Comparisons and ablations

Compare generation-0 base Nemotron, selected intermediate versions and the final checkpoint on the same testbed. Include buy-and-hold and a simple rule-based control.

| Variant | Strategy coordination and context | Data products |
| --- | --- | --- |
| Full | Meterless planning, shared memory and synthesis | Zetaris |
| Without Meterless | Declared plain-controller/log/context baseline; matched trial budget | Zetaris with the same inputs |
| Without Zetaris | Meterless unchanged | Equivalent joined data through a local SQL/file pipeline |
| Without both | Same plain-controller/context baseline | Same local replacement pipeline |

Match Nemotron settings, information, capital, development dates, candidate/token budgets and execution rules. The replacement pipeline must retain the same news, financial inputs, derived features and time limits. Measure query correctness, freshness, latency and replay/reset reliability alongside trading performance; a sponsor contribution need not appear as a profit increase in every run.

Plot **iteration/checkpoint versus net profit**, including base, intermediate and final checkpoints. With no cash flows, `net_profit = final_equity - K`, after costs and consistent valuation of open positions. Keep development-validation and sealed-test series separate. Also show equity over market time, drawdown, turnover, model cost and ablation results. Generate figures from evaluator records once experiments exist.

Evaluate query learning separately. Give base, intermediate and final checkpoints the same analysis prompts, schema, as-of database snapshot and call/token budget. Score executable and semantically correct SQL, time safety, evidence coverage, interpretation, unnecessary calls and latency; compare adaptive querying with fixed query templates. Judge equivalent results rather than exact SQL text. Add a controlled comparison of earlier/later eligible data snapshots crossed with base/learned query policies to distinguish more available data from better use of it. This information-growth experiment is separate from the equal-information tool ablations above. Plot query quality by generation alongside trading results, since higher profit alone does not establish better querying.

## Demonstration and evidence

A useful demo follows one linked example:

1. Show independently registered sources and a Zetaris query joining relevant records under a known cutoff.
2. Show Meterless recalling a prior decision/outcome and analytical rule, and how that changes the next Zetaris question or SQL query.
3. Show a valid Nemotron action, the simulator's execution/outcome, the updated history and the comparison with the parent strategy.

Compare a base and learned query on the same frozen analysis task, including a failed query or misleading correlation and its recorded correction.

Expose query/source references from decisions and revisions. Demonstrate that a future article stays unavailable and that a test-only memory marker disappears after checkpoint restoration. Include failures and regressions in the experiment record.

## Build order and project documents

1. Register and validate the primary Zetaris sources, shared definitions and joined evidence products.
2. Build the time-controlled environment and verify it using a scripted agent.
3. Connect the pinned Nemotron serving route and fixed JSON action interface.
4. Connect Meterless orchestration/memory and complete an evaluated parent-to-child strategy revision.
5. Run chronological development, the sealed checkpoint/ablation protocol, and evaluator-derived plots.

Detailed design: [AI-Trader-Architecture.md](AI-Trader-Architecture.md). Implementation stages and checks: [AI-Trader-Build-Guide.txt](AI-Trader-Build-Guide.txt).

## Hackathon fit and rule verification

The [public event page](https://hackathon.genai.works/event/open-agent-hackathon-2026) identifies Reasoning Architecture as Track 03. The proposed demonstration connects fragmented data, knowledge, memory, reasoning, actions and measured feedback. The [programme](https://academy.genai.works/courses/open-agent-hackathon-2026/details) also describes Zetaris, Meterless and Nemotron in its technology layers.

The source counts, data-product names, memory tests and experiment protocol above are our design acceptance criteria. They are not presented as quotations of hackathon rules.

The [linked Google rules document and selected tab](https://docs.google.com/document/d/1_d3osxbXZRpp4Ln80w6Obc5xx06SmkIMrgp6BOj0Ofc/edit?tab=t.17bg67fvhn1r) could not be read through the available retrieval route. Its document-specific requirements remain unverified. Check that source before claiming complete compliance or finalizing submission-format and sponsor-specific obligations.

## Primary references

- [Zetaris Cloud](https://www.zetaris.com/cloud) and [AgentFlow](https://www.zetaris.com/agentflow)
- [Zetaris SQL Guide](https://kbase.zetaris.com/knowledge/sql-guide) and [row/column access controls](https://kbase.zetaris.com/knowledge/controlling-row-level-access)
- [NVIDIA Nemotron usage example](https://docs.nvidia.com/nemotron/0.1.0/use-case-examples/Simple%20Nemotron-3-Nano%20Usage%20Example/README.html)
- [HKUDS AI-Trader](https://github.com/HKUDS/AI-Trader)
- [Co-LMLM: Continuous-Query Limited Memory Language Models](https://arxiv.org/abs/2607.07707)
