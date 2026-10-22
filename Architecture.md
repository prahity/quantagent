# AI Trader: Architecture with Meterless and Zetaris

Oct 21, 2026 · @Sai Ramsankar

## What each tool is good for

Meterless runs the agent loop; Zetaris is where the agent queries and analyzes price history and dated news. The simulator is completely separate from both.

**Meterless (agent side).** [Meterless Swarms](https://github.com/Meterless/Swarms) is a local-first multi-agent engine, shipped as a desktop app and a hostable web build. It splits one task into a dependency graph of specialist agents, runs them in parallel, merges their outputs and verifies the result. Every plan, prompt, model choice, intermediate output and verification report is inspectable ([v0.1.9 release](https://github.com/Meterless/Swarms/releases/tag/v0.1.9)). Its sibling app Gaia keeps projects, decisions and research across sessions ([overview](https://chatgate.ai/post/meterless-ai)), a natural home for what each iteration tried and how it scored.

**Zetaris (data side).** The [Zetaris AI Data Harness](https://pulse2.com/zetaris-launches-cloud-based-ai-data-harness-and-adds-steve-nouri-as-ai-strategy-advisor/) gives AI agents governed, real-time access to data without moving it first. [Zetaris](https://www.zetaris.com/agentflow) can also build Iceberg-format data lakes on storage of your choice, so the agent's analysts can query prices and scraped news in one place.

## How the pieces connect

Each iteration, the swarm analyzes data in Zetaris and revises a strategy, a separate simulator scores it, and Gaia carries the result into the next round.

&#91;embedded content: iteration loop · data, simulator, swarm, memory\]

1. **Zetaris is the agent's analysis source.** OHLCV price history plus a news table where every row has a `published_at` timestamp. The analysts query it to find patterns and read the news around each date.
2. **The simulator is completely separate.** Your own deterministic backtest engine, running on a frozen local snapshot of prices. It never touches Zetaris or the agent's code. It takes a strategy spec, steps through time, executes orders and returns a score report.
3. **Meterless runs the improvement loop.** Each iteration is a swarm of four roles: market analyst, news analyst, strategy proposer and critic/verifier. The analysts pull what they need from Zetaris; the proposer turns that analysis and the last run's results into a revised strategy spec.
4. **Results go back into Gaia** and become the next iteration's starting point.

Only two things cross between agent and simulator: the strategy spec going in and the score report coming out.

Have the agent edit a *strategy* (rules and parameters), not make each trade through an LLM call. A few years of daily bars would otherwise mean thousands of model calls per iteration: slow, expensive and non-reproducible.

## The traps

Four design issues will quietly inflate results unless the system is built around them from day one.

**Lookahead bias in the news.** Scraping news "for March 12, 2020" today returns mostly articles written afterward, with hindsight baked in. The agent must only see articles whose `published_at` is strictly before the decision time. Use sources that keep original publication times, such as dated news archives or GDELT, and store those timestamps in Zetaris. Because the agent queries Zetaris directly, cap every agent query at the end of its training window, using a read-only view or a wrapper that adds the date filter. A subtler leak: the LLM already knows what happened in 2020. The only honest test is on periods after the model's training cutoff, or with tickers and dates anonymized.

**Overfitting to "beat the last profit."** If every iteration is scored on the same history, the agent curve-fits it. The profit number climbs while real skill doesn't. To prevent this:

- Use walk-forward splits: tune on a training window, score on a validation window, keep a locked test period you rarely touch.
- Optimize a risk-adjusted metric such as Sharpe ratio or max drawdown, not raw total profit, so the agent doesn't learn to just take bigger risks.

**Simulator realism.** Include commissions and slippage. Fill orders at the next bar's price, not the bar that produced the signal. Be explicit about shorting and position limits. Without these, small edges vanish once costs apply.

**Integration check.** Swarms is primarily a desktop and browser app. Confirm it can be triggered programmatically in a loop before committing. If it can't, your own code drives the loop and calls the models directly, and Swarms becomes where you design and inspect the agent graph.

## Next steps

- [ ] Confirm Meterless Swarms can be called programmatically in a loop
- [ ] Scaffold a Python backtest engine with the as-of-date news filter and walk-forward scoring
- [ ] Stub the Zetaris queries and agent calls, then wire them in

## Sources

- [Meterless Swarms (GitHub)](https://github.com/Meterless/Swarms)
- [Meterless Swarms v0.1.9 release](https://github.com/Meterless/Swarms/releases/tag/v0.1.9)
- [Meterless.ai overview (ChatGate)](https://chatgate.ai/post/meterless-ai)
- [Zetaris AgentFlow](https://www.zetaris.com/agentflow)
- [Zetaris launches AI Data Harness (Pulse 2.0)](https://pulse2.com/zetaris-launches-cloud-based-ai-data-harness-and-adds-steve-nouri-as-ai-strategy-advisor/)
