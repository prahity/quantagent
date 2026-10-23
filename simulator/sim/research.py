"""Offline research specialists operating only on the released Feed.

Network retrieval and LLM synthesis live in research/, outside the evaluator.
These deterministic specialists make historical runs reproducible without keys.
"""
from __future__ import annotations

import hashlib
import json
import math

from pydantic import ValidationError

from contracts.research import Evidence, Finding, LiveCollection, ResearchBrief, ResearchConfig
from sim.feed import Feed


def build_brief(feed: Feed, *, config: ResearchConfig, run_id: str, request_id: str,
                symbols: list[str], data_version: str,
                collection: LiveCollection | None = None) -> ResearchBrief:
    cutoff = feed.clock.to_pydatetime()
    sources: list[Evidence] = []
    findings: list[Finding] = []
    gaps: list[str] = []
    news = feed.news(symbols=symbols).head(config.max_news)

    # Catalyst specialist: retain source text; do not infer sentiment from a filing title.
    for row in news.to_dict(orient="records"):
        if not row.get("availability_verified", False):
            gaps.append(f"Unverified availability excluded: {row['event_id']}")
            continue
        try:
            source = Evidence(
                source_id=f"news:{row['event_id']}", kind="news", symbol=row["symbol"],
                title=str(row["title"]), content=str(row.get("summary") or row["title"])[:2000],
                url=row.get("source_url") or None, provider=str(row["provider"]),
                published_at=row["published_at"], available_at=row["available_at"],
                released_at=row["released_at"],
            )
        except ValidationError:
            gaps.append(f"Invalid evidence excluded: {row['event_id']}")
            continue
        if source.eligible(cutoff):
            sources.append(source)
            findings.append(Finding(agent="catalysts", symbol=source.symbol,
                                    statement=source.title[:1600], source_ids=[source.source_id]))

    # Market and risk specialists: describe observed returns, never predicted returns.
    for symbol in symbols:
        bars = feed.prices(symbol, limit=config.price_bars)
        if len(bars) < 2:
            gaps.append(f"{symbol}: fewer than two completed price bars.")
            continue
        first, last = bars.iloc[0], bars.iloc[-1]
        closes = bars.close.astype(float)
        if (closes <= 0).any() or not closes.map(math.isfinite).all():
            gaps.append(f"{symbol}: invalid close prices excluded.")
            continue
        observed_return = (float(last.close) / float(first.close) - 1) * 100
        drawdown = float((closes / closes.cummax() - 1).min() * 100)
        source_id = f"prices:{symbol}:{last.bar_end.isoformat()}"
        sources.append(Evidence(
            source_id=source_id, kind="price", symbol=symbol,
            title=f"{symbol}: {len(bars)} completed bars",
            content=json.dumps({"bars": json.loads(bars.to_json(orient="records", date_format="iso"))}),
            provider="snapshot", published_at=last.bar_end,
            available_at=last.bar_end, released_at=last.bar_end,
        ))
        findings.append(Finding(agent="market", symbol=symbol, source_ids=[source_id],
            statement=f"Observed close-to-close return: {observed_return:+.2f}% across {len(bars)} completed bars."))
        findings.append(Finding(agent="risk", symbol=symbol, source_ids=[source_id],
            statement=f"Largest close-based drawdown in this {len(bars)}-bar window: {drawdown:.2f}%. This is not a forecast."))

    if collection is not None:
        # Gate the entire derived collection by completion time, including inference latency.
        if collection.available_at <= cutoff:
            extra = [s for s in collection.sources if s.eligible(cutoff) and (s.symbol is None or s.symbol in symbols)]
            ids = {s.source_id for s in extra}
            sources.extend(extra)
            findings.extend(f for f in collection.findings
                            if set(f.source_ids) <= ids and (f.symbol is None or f.symbol in symbols))
            cited = {sid for f in collection.findings for sid in f.source_ids}
            for source in extra:
                if source.source_id not in cited:
                    findings.append(Finding(agent="catalysts", symbol=source.symbol,
                                            statement=source.title[:1600], source_ids=[source.source_id]))
            gaps.extend(collection.warnings)
        else:
            gaps.append("Attached live collection is unavailable at this decision time.")

    if not any(s.kind in {"news", "web"} for s in sources):
        gaps.append("No verified news or filings are available at this cutoff.")
    gaps.append("Historical research is extractive and rule-based; no web search or model calls occur during replay.")
    body = dict(run_id=run_id, request_id=request_id, as_of=cutoff, data_version=data_version,
                collection_id=collection.collection_id if collection is not None else None,
                synthesis_model=collection.synthesis_model if collection is not None and collection.available_at <= cutoff else None,
                sources=sources, findings=findings, gaps=gaps)
    digest = hashlib.sha256(json.dumps(body, default=lambda x: x.model_dump(mode="json")
                                      if hasattr(x, "model_dump") else x.isoformat(), sort_keys=True).encode()).hexdigest()[:20]
    return ResearchBrief(brief_id=f"brief_{digest}", **body)
