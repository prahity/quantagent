"""Bounded live research collection and immutable archive storage.

No simulated cutoff is accepted here. Collection always uses wall-clock time.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, ConfigDict, Field

from contracts.research import LiveCollection
from research.providers import ExaSearch, FireworksSynthesis, ProviderError
from sim.research_archive import ResearchArchive  # re-export for collection callers


class CollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(min_length=1, max_length=2)
    lookback_days: int = Field(default=7, ge=1, le=30)
    max_results: int = Field(default=3, ge=1, le=5)
    synthesize: bool = False


def provider_status() -> dict:
    return {
        "exa_configured": bool(os.getenv("EXA_API_KEY")),
        "fireworks_configured": bool(os.getenv("FIREWORKS_API_KEY") and os.getenv("FIREWORKS_MODEL")),
        "collection_enabled": bool(os.getenv("RESEARCH_API_TOKEN")),
        "max_symbols": 2, "max_search_calls": 5, "timeout_seconds": 55,
        "historical_mode": "snapshot-specialists-v1",
    }


async def collect(request: CollectRequest, company_names: dict[str, str],
                  client: httpx.AsyncClient | None = None) -> LiveCollection:
    key = os.getenv("EXA_API_KEY", "")
    if not key:
        raise ProviderError("EXA_API_KEY is not configured.")
    symbols = list(dict.fromkeys(s.upper().strip() for s in request.symbols))
    if any(s not in company_names for s in symbols):
        raise ValueError("research symbols must belong to the configured universe")
    started = datetime.now(timezone.utc)
    jobs = []
    for symbol in symbols:
        company = company_names[symbol]
        jobs.extend([
            (f"{company} {symbol} earnings company announcements material business developments", symbol, "news"),
            (f"{company} {symbol} SEC filings financial risks earnings guidance", symbol, "financial report"),
        ])
    jobs.append(("US equity markets Federal Reserve inflation interest rates macroeconomic risks", None, "news"))

    owned = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), follow_redirects=False)
    warnings: list[str] = []
    sources = []
    findings = []
    model = None
    try:
        async with asyncio.timeout(55):
            exa = ExaSearch(key, client)
            results = await asyncio.gather(*(
                exa.search(query=query, symbol=symbol, category=category, now=started,
                           lookback_days=request.lookback_days, max_results=request.max_results)
                for query, symbol, category in jobs
            ), return_exceptions=True)
            seen = set()
            for result in results:
                if isinstance(result, Exception):
                    warnings.append(str(result) if isinstance(result, ProviderError) else "One research search failed.")
                    continue
                records, excluded = result
                if excluded:
                    warnings.append(f"Excluded {excluded} undated, invalid, or out-of-window search results.")
                for source in records:
                    # Keep the same URL separately for different symbol scopes.
                    identity = (str(source.url), source.symbol)
                    if identity not in seen:
                        seen.add(identity)
                        sources.append(source)
            if request.synthesize and sources:
                fw_key, fw_model = os.getenv("FIREWORKS_API_KEY"), os.getenv("FIREWORKS_MODEL")
                if not fw_key or not fw_model:
                    warnings.append("Fireworks synthesis requested but FIREWORKS_API_KEY/FIREWORKS_MODEL is missing.")
                else:
                    try:
                        result = await FireworksSynthesis(fw_key, fw_model, client).synthesize(sources, symbols)
                        findings = result.findings
                        warnings.extend(result.gaps)
                        model = fw_model
                    except ProviderError as exc:
                        warnings.append(str(exc))
    except TimeoutError:
        warnings.append("Research collection reached its 55-second time budget.")
    finally:
        if owned:
            await client.aclose()
    return LiveCollection(
        collection_id=f"research_{uuid.uuid4().hex}", symbols=symbols,
        started_at=started, available_at=datetime.now(timezone.utc), sources=sources,
        findings=findings, warnings=warnings, synthesis_model=model, search_calls=len(jobs),
        status="empty" if not sources else "partial" if warnings else "complete",
    )
