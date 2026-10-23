"""Versioned, sourced research context. Research never contains executable orders."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

TIME_BUCKET_FIELDS = {f"{name}_{bucket}" for name in ("published", "available", "released", "retrieved")
                      for bucket in ("day", "hour")}


class ResearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_news: int = Field(default=12, ge=1, le=40)
    price_bars: int = Field(default=20, ge=2, le=100)
    collection_id: str | None = Field(default=None, pattern=r"^research_[0-9a-f]{32}$")


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source_id: str
    kind: Literal["news", "price", "web"]
    symbol: str | None = None
    title: str
    content: str
    url: HttpUrl | None = None
    published_at: AwareDatetime
    available_at: AwareDatetime
    released_at: AwareDatetime
    retrieved_at: AwareDatetime | None = None
    provider: str
    published_day: date | None = None
    published_hour: AwareDatetime | None = None
    available_day: date | None = None
    available_hour: AwareDatetime | None = None
    released_day: date | None = None
    released_hour: AwareDatetime | None = None
    retrieved_day: date | None = None
    retrieved_hour: AwareDatetime | None = None

    @model_validator(mode="after")
    def utc_time_buckets(self):
        """Derive UTC buckets from source timestamps; never let callers invent them."""
        for name in ("published", "available", "released", "retrieved"):
            timestamp = getattr(self, f"{name}_at")
            timestamp = timestamp.astimezone(timezone.utc) if timestamp is not None else None
            setattr(self, f"{name}_at", timestamp)
            values = {"day": timestamp.date() if timestamp else None,
                      "hour": timestamp.replace(minute=0, second=0, microsecond=0) if timestamp else None}
            for bucket, expected in values.items():
                supplied = getattr(self, f"{name}_{bucket}")
                if supplied is not None and supplied != expected:
                    raise ValueError(f"{name}_{bucket} must match its UTC timestamp")
                setattr(self, f"{name}_{bucket}", expected)
        return self

    def eligible(self, cutoff: datetime) -> bool:
        times = [self.published_at, self.available_at, self.released_at]
        # A web publication date alone does not prove the retrieved version existed then.
        if self.kind == "web" and self.retrieved_at is not None:
            times.append(self.retrieved_at)
        return max(times) <= cutoff


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: Literal["catalysts", "market", "risk"]
    symbol: str | None = None
    statement: str = Field(min_length=1, max_length=1600)
    source_ids: list[str] = Field(min_length=1, max_length=12)
    interpretation: bool = False


class ResearchBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["research-1.0"] = "research-1.0"
    brief_id: str
    run_id: str
    request_id: str
    as_of: AwareDatetime
    data_version: str
    collection_id: str | None = None
    synthesis_model: str | None = None
    sources: list[Evidence]
    findings: list[Finding]
    gaps: list[str]
    method: Literal["snapshot-specialists-v1"] = "snapshot-specialists-v1"


class LiveCollection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["research-1.0"] = "research-1.0"
    collection_id: str = Field(pattern=r"^research_[0-9a-f]{32}$")
    symbols: list[str]
    started_at: AwareDatetime
    available_at: AwareDatetime
    sources: list[Evidence]
    findings: list[Finding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    synthesis_model: str | None = None
    status: Literal["complete", "partial", "empty"]
    search_calls: int

    @model_validator(mode="after")
    def check_provenance(self):
        ids = {s.source_id for s in self.sources}
        if len(ids) != len(self.sources):
            raise ValueError("duplicate evidence ids")
        for source in self.sources:
            if source.kind != "web" or source.retrieved_at is None:
                raise ValueError("live collections require retrieved web evidence")
            if not source.eligible(self.available_at):
                raise ValueError("source not eligible at collection completion")
        for finding in self.findings:
            if not set(finding.source_ids) <= ids:
                raise ValueError("finding references unknown evidence")
            if finding.symbol is not None and finding.symbol not in self.symbols:
                raise ValueError("finding references unknown symbol")
        return self
