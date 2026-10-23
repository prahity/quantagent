"""Chronological organization tests use provider doubles, never market demo data."""
import unittest

from pydantic import ValidationError

from contracts.research import Evidence, TIME_BUCKET_FIELDS
from sim.research_query import EvidenceQuery, query_evidence


def source(source_id, published, symbol="AAPL", **overrides):
    return Evidence(source_id=source_id, kind="web", symbol=symbol, title="Earnings release",
        content="Source fixture", provider="exa", published_at=published,
        available_at="2026-10-23T12:01:00Z", released_at="2026-10-23T12:01:00Z",
        retrieved_at="2026-10-23T12:00:00Z", **overrides)


class EvidenceQueryTests(unittest.TestCase):
    def setUp(self):
        self.sources = [source("a", "2025-01-28T23:40:00-05:00"),
                        source("b", "2025-01-29T04:10:00Z", "MSFT"),
                        source("c", "2025-01-29T05:00:00Z"),
                        source("d", "2025-01-30T00:00:00Z")]
        self.rows = [{"source": s} for s in self.sources]

    def test_utc_buckets_and_serialization_round_trip(self):
        first = self.sources[0]
        self.assertEqual(first.published_day.isoformat(), "2025-01-29")
        self.assertEqual(first.published_hour.isoformat(), "2025-01-29T04:00:00+00:00")
        self.assertEqual(first.retrieved_day.isoformat(), "2026-10-23")
        self.assertEqual(Evidence.model_validate_json(first.model_dump_json()), first)
        legacy = first.model_dump(mode="json", exclude=TIME_BUCKET_FIELDS)
        self.assertEqual(Evidence.model_validate(legacy), first)
        with self.assertRaises(ValidationError):
            Evidence.model_validate({**legacy, "published_day": "2025-01-28"})

    def test_day_hour_filter_is_exact_and_does_not_mix_days(self):
        out = query_evidence(self.rows, EvidenceQuery(day="2025-01-29", hour=4, sort_order="asc"))
        self.assertEqual([r["source_id"] for r in out["records"]], ["b", "a"])
        self.assertEqual(out["buckets"], [{"bucket": "2025-01-29T04:00:00+00:00", "count": 2}])
        self.assertEqual(query_evidence(self.rows, EvidenceQuery(day="2025-01-28"))["total"], 0)

    def test_exclusive_range_sort_and_pagination_counts(self):
        out = query_evidence(self.rows, EvidenceQuery(start="2025-01-29T04:00:00Z", end="2025-01-30T00:00:00Z",
            sort_order="asc", offset=1, limit=1, group_by="day"))
        self.assertEqual(out["total"], 3)
        self.assertEqual([r["source_id"] for r in out["records"]], ["a"])
        self.assertEqual(out["buckets"], [{"bucket": "2025-01-29", "count": 3}])

    def test_filters_and_timestamp_basis_are_independent(self):
        out = query_evidence(self.rows, EvidenceQuery(symbol="msft", provider="EXA", kind="web", text="EARNINGS"))
        self.assertEqual([r["source_id"] for r in out["records"]], ["b"])
        out = query_evidence(self.rows, EvidenceQuery(day="2026-10-23", hour=12, time_field="retrieved_at"))
        self.assertEqual(out["total"], 4)
        self.assertEqual(query_evidence(self.rows, EvidenceQuery(day="2026-10-23"))["total"], 0)

    def test_invalid_ranges_hours_and_sort_keys_are_rejected(self):
        for kwargs in ({"hour": 24}, {"hour": -1}, {"sort_by": "content"}, {"limit": 0},
                       {"start": "2025-01-29T00:00:00Z", "end": "2025-01-28T00:00:00Z"},
                       {"start": "2025-01-29T00:00:00"}):
            with self.assertRaises(ValidationError):
                EvidenceQuery(**kwargs)
