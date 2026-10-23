"""Day/hour organization shared by Exa archives and already-released run evidence."""
from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

class EvidenceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_field: Literal["published_at", "available_at", "released_at", "retrieved_at"] = "published_at"
    day: date | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    symbol: str | None = None
    provider: str | None = None
    kind: Literal["news", "web", "price"] | None = None
    text: str | None = Field(default=None, max_length=200)
    group_by: Literal["day", "hour"] = "hour"
    sort_by: Literal["published_at", "available_at", "released_at", "retrieved_at", "symbol", "provider"] = "published_at"
    sort_order: Literal["asc", "desc"] = "desc"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def validate_range(self):
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("end must be after start (end is exclusive)")
        return self


class RunEvidenceQuery(EvidenceQuery):
    request_id: str | None = None


def query_evidence(rows: list[dict], query: EvidenceQuery) -> dict:
    """Filter first, count complete UTC buckets, sort stably, then paginate.

    Each input row contains an Evidence under source, with optional collection_id.
    Only trusted archive callers may pass raw records; run callers pass emitted briefs.
    """
    selected = []
    for row in rows:
        source = row["source"]
        stamp = getattr(source, query.time_field)
        if stamp is None:  # e.g. snapshot bars have no retrieval timestamp
            continue
        if query.day is not None and stamp.date() != query.day:
            continue
        if query.hour is not None and stamp.hour != query.hour:
            continue
        if query.start is not None and stamp < query.start:
            continue
        if query.end is not None and stamp >= query.end:
            continue
        if query.symbol is not None and source.symbol != query.symbol.strip().upper():
            continue
        if query.provider is not None and source.provider.casefold() != query.provider.casefold():
            continue
        if query.kind is not None and source.kind != query.kind:
            continue
        if query.text is not None and query.text.casefold() not in (source.title + " " + source.content).casefold():
            continue
        selected.append(row)

    counts = Counter()
    for row in selected:
        stamp = getattr(row["source"], query.time_field)
        bucket = stamp.date().isoformat() if query.group_by == "day" else stamp.replace(minute=0, second=0, microsecond=0).isoformat()
        counts[bucket] += 1

    # Stable tie breaker across repeated URLs/versions/collections. Null sort values stay last.
    selected.sort(key=lambda row: (row["source"].source_id, row.get("collection_id") or ""))
    present = [r for r in selected if getattr(r["source"], query.sort_by) is not None]
    missing = [r for r in selected if getattr(r["source"], query.sort_by) is None]
    present.sort(key=lambda row: getattr(row["source"], query.sort_by), reverse=query.sort_order == "desc")
    ordered = present + missing
    records = [{**row["source"].model_dump(mode="json"),
                **({"collection_id": row["collection_id"]} if row.get("collection_id") else {})}
               for row in ordered[query.offset:query.offset + query.limit]]
    return {"timezone": "UTC", "time_field": query.time_field, "group_by": query.group_by,
            "sort_by": query.sort_by, "sort_order": query.sort_order,
            "total": len(selected), "offset": query.offset, "limit": query.limit,
            "buckets": [{"bucket": bucket, "count": counts[bucket]} for bucket in sorted(counts)],
            "records": records}
