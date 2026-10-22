"""Build the frozen snapshot under sim/snapshot/ from real public sources.

    python scripts/fetch_data.py            # prices + SEC filings (+ keyed news providers if keys set)
    python scripts/fetch_data.py --fixtures # also refresh the small real-data test fixtures

Environment (all optional):
    SEC_USER_AGENT          "Your Name your@email"  (the SEC asks for a contact in the UA)
    ALPHAVANTAGE_API_KEY    free key -> adds Alpha Vantage news articles
    FINNHUB_API_KEY         free key -> adds Finnhub company news
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from sim.config import MANIFEST_PATH, SNAPSHOT_DIR, load_universe  # noqa: E402
from sim.data.load_news import fetch_universe_news  # noqa: E402
from sim.data.load_prices import fetch_universe_bars  # noqa: E402
from sim.data.manifest import build_manifest, write_manifest  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures"


def write_fixtures(bars: dict[str, pd.DataFrame], news: pd.DataFrame) -> None:
    """Small slices of the real snapshot so tests run without the full download."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    lo, hi = pd.Timestamp("2025-01-27", tz="UTC"), pd.Timestamp("2025-02-08", tz="UTC")
    symbols = ["AAPL", "MSFT", "SPY"]
    for interval, frame in bars.items():
        sub = frame[(frame.symbol.isin(symbols)) & (frame.ts >= lo) & (frame.ts < hi)]
        sub.to_parquet(FIXTURE_DIR / f"bars_{interval}.parquet", index=False)
        print(f"  fixture bars_{interval}: {len(sub)} rows")
    sub_news = news[(news.symbol.isin(symbols)) & (news.available_at >= lo - pd.Timedelta(days=7)) & (news.available_at < hi)]
    sub_news.to_parquet(FIXTURE_DIR / "news.parquet", index=False)
    print(f"  fixture news: {len(sub_news)} rows")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", action="store_true", help="also write tests/fixtures from the fetched data")
    parser.add_argument("--skip-news", action="store_true", help="reuse the existing news.parquet")
    parser.add_argument("--skip-prices", action="store_true", help="reuse the existing bars_*.parquet")
    args = parser.parse_args()

    universe = load_universe()
    symbols = [s["ticker"] for s in universe["symbols"]]
    start, end = universe["data_start"], universe["data_end"]
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    if args.skip_prices:
        bars = {i: pd.read_parquet(SNAPSHOT_DIR / f"bars_{i}.parquet") for i in ("hourly", "daily")}
    else:
        print(f"Prices: {len(symbols)} symbols, {start} -> {end}, Yahoo Finance chart API")
        bars = fetch_universe_bars(symbols, start, end)
        for interval, frame in bars.items():
            frame.to_parquet(SNAPSHOT_DIR / f"bars_{interval}.parquet", index=False)

    if args.skip_news:
        news_path = SNAPSHOT_DIR / "news.parquet"
        news = pd.read_parquet(news_path) if news_path.exists() else pd.DataFrame()
    else:
        print("News/context: SEC EDGAR submissions API (+ keyed providers when configured)")
        news = fetch_universe_news(universe["symbols"], start, end)
        news.to_parquet(SNAPSHOT_DIR / "news.parquet", index=False)

    sources = {
        "prices": {"provider": "yahoo_finance_chart_api", "intervals": ["hourly (1h, unadjusted)", "daily (1d, adj_close included)"], "session": "regular hours only"},
        "news": {
            "sec_edgar": "data.sec.gov/submissions; published_at = acceptance time corrected to true UTC (API value minus ET offset, verified against filing index pages); available_at = acceptance, or 06:00 ET on a later filing date",
            "alpha_vantage": "only when ALPHAVANTAGE_API_KEY is set; provider timestamps, unverified",
            "finnhub": "only when FINNHUB_API_KEY is set; provider timestamps, unverified",
        },
    }
    manifest = build_manifest(SNAPSHOT_DIR, bars, news, universe, sources)
    write_manifest(MANIFEST_PATH, manifest)
    print(f"Manifest written: {MANIFEST_PATH} data_version={manifest['data_version']}")

    if args.fixtures:
        write_fixtures(bars, news)


if __name__ == "__main__":
    main()
