"""Offline research reader. Loading an archive never makes a network request."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

UTC = timezone.utc
DEFAULT_ARCHIVE = Path(__file__).resolve().parents[1] / "research_archive"


def utc(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("a timezone-aware timestamp is required")
    return parsed.astimezone(UTC)


class Article(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=r"^exa:[0-9a-f]{24}$")
    provider: Literal["exa"] = "exa"
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(max_length=4096)
    excerpt: str = Field(min_length=1, max_length=2000)
    published_at_raw: str
    published_at: AwareDatetime | None
    published_date: date
    retrieved_at: AwareDatetime
    available_at: AwareDatetime
    released_at: AwareDatetime

    @model_validator(mode="after")
    def valid_metadata(self):
        url = urlsplit(self.url)
        if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password:
            raise ValueError("article must have an HTTP(S) source URL without credentials")
        if self.published_at is not None:
            if utc(self.published_at).date() != self.published_date:
                raise ValueError("publication date must match the UTC timestamp")
            if utc(self.published_at_raw) != utc(self.published_at):
                raise ValueError("raw publication timestamp mismatch")
            if self.published_at > self.retrieved_at:
                raise ValueError("publication is after retrieval")
        elif self.published_at_raw != self.published_date.isoformat():
            raise ValueError("date-only publication metadata mismatch")
        if self.published_date > utc(self.retrieved_at).date():
            raise ValueError("publication date is after retrieval")
        if not self.retrieved_at <= self.available_at <= self.released_at:
            raise ValueError("retrieval, availability, and release must be chronological")
        return self

    @property
    def published_day(self) -> str:
        return self.published_date.isoformat()

    @property
    def published_hour(self) -> str | None:
        # A date without a time belongs to the day, never a fabricated midnight hour.
        return utc(self.published_at).strftime("%Y-%m-%dT%H:00:00Z") if self.published_at else None

    @property
    def publication_cutoff(self) -> datetime:
        # With an unknown hour, conservatively wait until the entire day has ended.
        return utc(self.published_at) if self.published_at else datetime.combine(self.published_date + timedelta(days=1), time.min, UTC)

    def as_dict(self) -> dict:
        return {**self.model_dump(mode="json"), "published_day": self.published_day, "published_hour": self.published_hour}


class Collection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    collection_id: str = Field(pattern=r"^exa_[0-9a-f]{32}$")
    started_at: AwareDatetime
    completed_at: AwareDatetime
    queries: dict[str, str]
    lookback_days: int = Field(ge=1, le=30)
    search_calls: int = Field(ge=1, le=8)
    articles: list[Article] = Field(max_length=80)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_release(self):
        if self.completed_at < self.started_at:
            raise ValueError("collection completed before it started")
        if any(not self.started_at <= a.retrieved_at <= a.released_at <= self.completed_at for a in self.articles):
            raise ValueError("article timestamps are outside the collection")
        return self


def save_collection(collection: Collection, root: Path = DEFAULT_ARCHIVE) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{collection.collection_id}.json"
    # Exclusive create preserves prior versions rather than overwriting them.
    with path.open("x") as stream:
        stream.write(collection.model_dump_json(indent=2))
    return path


class ResearchArchive:
    """Pin collection bytes once at run start; later collections cannot change a replay."""

    def __init__(self, root: Path):
        if not root.is_dir():
            raise ValueError(f"research archive directory does not exist: {root}")
        self.collections: list[Collection] = []
        self.manifest: list[dict] = []
        for path in sorted(root.glob("exa_*.json")):
            raw = path.read_bytes()
            collection = Collection.model_validate_json(raw)
            if path.stem != collection.collection_id:
                raise ValueError("research collection filename/id mismatch")
            self.collections.append(collection)
            self.manifest.append({"collection_id": collection.collection_id, "sha256": hashlib.sha256(raw).hexdigest()})

    def query(self, *, as_of: str | datetime, symbols: list[str] | None = None,
              day: str | None = None, hour: int | None = None, limit: int = 100,
              lookback_days: int | None = None, newest_first: bool = True) -> list[dict]:
        cutoff = utc(as_of)
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if hour is not None and (day is None or not 0 <= hour <= 23):
            raise ValueError("an hour filter requires a day and an hour from 0 to 23")
        if day is not None:
            date.fromisoformat(day)
        if lookback_days is not None and not 1 <= lookback_days <= 30:
            raise ValueError("lookback_days must be between 1 and 30")
        wanted = {s.upper() for s in symbols} if symbols is not None else None
        rows = []
        for batch in self.collections:
            if batch.completed_at > cutoff:
                continue
            for article in batch.articles:
                if wanted is not None and article.symbol not in wanted:
                    continue
                if max(article.publication_cutoff, article.retrieved_at, article.available_at, article.released_at) > cutoff:
                    continue
                if lookback_days is not None and article.publication_cutoff < cutoff - timedelta(days=lookback_days):
                    continue
                if day is not None and article.published_day != day:
                    continue
                if hour is not None and (article.published_at is None or utc(article.published_at).hour != hour):
                    continue
                rows.append((article, batch.collection_id))
        # Keep the latest eligible captured version of a URL for each symbol.
        rows.sort(key=lambda row: (row[0].retrieved_at, row[0].source_id, row[1]), reverse=True)
        seen = set()
        unique = []
        for article, collection_id in rows:
            key = (article.url, article.symbol)
            if key not in seen:
                seen.add(key)
                unique.append((article, collection_id))
        unique.sort(key=lambda row: (row[0].publication_cutoff, row[0].source_id), reverse=newest_first)
        return [{**article.as_dict(), "collection_id": cid} for article, cid in unique[:limit]]

    def for_request(self, request: dict) -> dict:
        symbols = (request.get("data") or {}).get("symbols") or []
        return {
            "as_of": request["as_of"],
            "sources": self.query(as_of=request["as_of"], symbols=symbols, limit=6, lookback_days=7),
        }


def open_research_archive(path: str | Path | None = None) -> ResearchArchive | None:
    configured = path if path is not None else os.getenv("EXA_RESEARCH_ARCHIVE")
    return ResearchArchive(Path(configured).expanduser().resolve()) if configured else None


def write_research_audit(agent, run_dir: Path) -> None:
    """Retain the exact released excerpts and pinned hashes beside evaluator results."""
    archive = getattr(agent, "research", None)
    if archive is None:
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "research_manifest.json").write_text(json.dumps(archive.manifest, indent=2))
    with (run_dir / "research.jsonl").open("w") as stream:
        for row in agent.research_log:
            stream.write(json.dumps(row) + "\n")
