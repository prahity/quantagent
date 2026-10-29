"""Collect market research with Exa, or inspect the local archive without network access."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from research.archive import DEFAULT_ARCHIVE, ResearchArchive, save_collection
from research.exa import ResearchError, collect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gather = sub.add_parser("collect", help="make one Exa search per symbol and save an immutable collection")
    gather.add_argument("--symbols", default="AAPL,MSFT,SPY")
    gather.add_argument("--lookback-days", type=int, default=7)
    gather.add_argument("--max-results", type=int, default=3)
    gather.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    read = sub.add_parser("list", help="filter archived sources; never calls Exa")
    read.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    read.add_argument("--symbols", default="")
    read.add_argument("--as-of", default=None, help="timezone-aware ISO timestamp; defaults to now")
    read.add_argument("--day", help="UTC YYYY-MM-DD")
    read.add_argument("--hour", type=int, help="UTC 0–23; requires --day")
    read.add_argument("--limit", type=int, default=100)
    read.add_argument("--oldest-first", action="store_true")
    args = parser.parse_args(argv)
    try:
        symbols = list(dict.fromkeys(s.strip().upper() for s in args.symbols.split(",") if s.strip()))
        if args.command == "collect":
            config = Path(__file__).resolve().parents[1] / "simulator/config/universe.yaml"
            universe = {row["ticker"]: row["name"] for row in yaml.safe_load(config.read_text())["symbols"]}
            if not symbols or any(s not in universe for s in symbols):
                raise ValueError("symbols must belong to simulator/config/universe.yaml")
            batch = collect({s: universe[s] for s in symbols}, lookback_days=args.lookback_days, max_results=args.max_results)
            path = save_collection(batch, args.archive)
            print(json.dumps({"collection_id": batch.collection_id, "path": str(path), "articles": len(batch.articles),
                              "search_calls": batch.search_calls, "warnings": batch.warnings}, indent=2))
            return 0 if batch.articles else 1
        archive = ResearchArchive(args.archive)
        rows = archive.query(as_of=args.as_of or datetime.now(timezone.utc), symbols=symbols or None,
                             day=args.day, hour=args.hour, limit=args.limit, newest_first=not args.oldest_first)
        print(json.dumps({"count": len(rows), "sources": rows}, indent=2))
        return 0
    except (ResearchError, ValueError, OSError) as exc:
        # Do not echo malformed archive contents or arbitrary provider errors.
        message = str(exc) if isinstance(exc, ResearchError) else "Invalid research arguments or archive; check the path, symbols, and UTC filters."
        parser.exit(2, message + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
