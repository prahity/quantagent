"""Bounded Exa ingestion. Run outside the trading decision loop."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
from pydantic import ValidationError

from research.archive import Article, Collection, utc

EXA_URL = "https://api.exa.ai/search"


class ResearchError(RuntimeError):
    """Messages must not include provider bodies, request headers, or credentials."""


def _article(item: dict, symbol: str, retrieved: datetime, start: datetime, end: datetime) -> Article:
    raw = item["publishedDate"]
    if not isinstance(raw, str):
        raise ValueError("publication date is missing")
    if len(raw) == 10:
        published, day = None, date.fromisoformat(raw)
    else:
        published = utc(raw)
        day = published.date()
    if not start.date() <= day <= end.date() or (published and not start <= published <= end):
        raise ValueError("outside publication window")
    highlights = item.get("highlights")
    excerpt = "\n".join(h for h in highlights if isinstance(h, str)) if isinstance(highlights, list) else ""
    if not excerpt.strip():
        excerpt = item.get("text", "")
    if not isinstance(excerpt, str) or not excerpt.strip():
        raise ValueError("missing contents")
    title = item["title"]
    if not isinstance(title, str) or not title.strip():
        raise ValueError("missing title")
    excerpt = excerpt.strip()[:2000]
    fingerprint = json.dumps([item["url"], symbol, raw, excerpt], ensure_ascii=False)
    return Article(
        source_id="exa:" + hashlib.sha256(fingerprint.encode()).hexdigest()[:24],
        symbol=symbol, title=title.strip()[:500], url=item["url"], excerpt=excerpt,
        published_at_raw=raw, published_at=published, published_date=day,
        retrieved_at=retrieved, available_at=retrieved, released_at=retrieved,
    )


def collect(companies: dict[str, str], *, lookback_days: int = 7, max_results: int = 3,
            api_key: str | None = None, client: httpx.Client | None = None) -> Collection:
    """One search per symbol, no automatic retries. All timestamps use the real clock."""
    key = api_key if api_key is not None else os.getenv("EXA_API_KEY", "")
    if not key.strip():
        raise ResearchError("EXA_API_KEY is not configured.")
    if not 1 <= len(companies) <= 8 or not 1 <= lookback_days <= 30 or not 1 <= max_results <= 10:
        raise ValueError("use 1–8 symbols, 1–30 lookback days, and 1–10 results per symbol")
    if any(not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol) or not name.strip() for symbol, name in companies.items()):
        raise ValueError("company names and valid uppercase symbols are required")
    started = datetime.now(timezone.utc)
    start = started - timedelta(days=lookback_days)
    queries = {
        symbol: f"{company} ({symbol}) earnings guidance company announcements material financial developments and risks in the past {lookback_days} days"
        for symbol, company in companies.items()
    }
    articles, warnings = [], []
    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False)
    try:
        for symbol, query in queries.items():
            try:
                response = client.post(EXA_URL, headers={"x-api-key": key}, json={
                    "query": query, "type": "auto", "numResults": max_results,
                    "startPublishedDate": start.isoformat(), "endPublishedDate": started.isoformat(),
                    "contents": {"highlights": True},
                }, timeout=15)
                if response.status_code != 200:
                    warnings.append(f"{symbol}: Exa returned HTTP {response.status_code}.")
                    continue
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                    raise ValueError("invalid results")
                retrieved = datetime.now(timezone.utc)
                excluded = 0
                for item in body["results"][:max_results]:
                    try:
                        if not isinstance(item, dict):
                            raise ValueError("invalid result")
                        articles.append(_article(item, symbol, retrieved, start, started))
                    except (KeyError, TypeError, ValueError, ValidationError):
                        excluded += 1
                if excluded:
                    warnings.append(f"{symbol}: excluded {excluded} undated, invalid, or out-of-window results.")
            except httpx.TimeoutException:
                warnings.append(f"{symbol}: Exa timed out.")
            except (httpx.HTTPError, ValueError):
                warnings.append(f"{symbol}: Exa returned an unavailable or invalid response.")
    finally:
        if owned:
            client.close()
    completed = datetime.now(timezone.utc)
    seen = set()
    unique = []
    for article in articles:
        key_id = (article.url, article.symbol)
        if key_id not in seen:
            seen.add(key_id)
            unique.append(article.model_copy(update={"released_at": completed}))
    unique.sort(key=lambda article: (article.publication_cutoff, article.source_id))
    return Collection(collection_id="exa_" + uuid.uuid4().hex, started_at=started, completed_at=completed,
                      queries=queries, lookback_days=lookback_days, search_calls=len(queries),
                      articles=unique, warnings=warnings)
