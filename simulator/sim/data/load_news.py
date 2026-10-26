"""Collect timestamped news/context records for the universe.

Primary source (no key, verifiable timestamps): SEC EDGAR filings via the
``data.sec.gov/submissions`` API. ``published_at`` is the EDGAR acceptance timestamp.
Availability is conservative: if EDGAR assigned a later filing date (filings accepted
after 17:30 ET), ``available_at`` becomes 06:00 ET on that filing date.

Optional keyed providers (free tiers) are used only when their environment variable is
set. Their publication timestamps cannot be independently verified, so records carry
``availability_basis = provider_timestamp`` and ``availability_verified = False``.

Every record keeps ``retrieved_at`` (our collection time). It never counts as
historical availability. ``released_at`` is stamped by the simulator at run time.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

ET = ZoneInfo("America/New_York")
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/{name}"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
DEFAULT_SEC_UA = "ai-trader-sim research (set SEC_USER_AGENT=name contact@email)"

DEFAULT_FORMS = ("8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A", "N-CSR", "N-CSRS")

ITEM_8K = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "3.02": "Unregistered Sales of Equity Securities",
    "5.02": "Departure/Election of Directors or Officers; Compensatory Arrangements",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

NEWS_COLUMNS = [
    "event_id", "symbol", "provider", "form", "title", "summary", "source_url",
    "published_at", "available_at", "retrieved_at", "availability_basis",
    "availability_verified", "content_version", "filing_date", "items", "acceptance_api_raw",
]


def sec_acceptance_to_utc(api_value: str) -> pd.Timestamp:
    """Parse the submissions-API ``acceptanceDateTime`` as true UTC.

    Until October 2026 the API returned the Eastern acceptance time plus twice the
    ET/UTC offset, and this function subtracted one offset. The API now returns real
    UTC (index ``2025-01-30 16:30:23`` ET -> API ``2025-01-30T21:30:23Z``; index
    ``2025-01-22 17:15:39`` ET -> API ``2025-01-22T22:15:39Z``, checked 2026-10-25).
    Applying the old correction now dates filings 4-5 hours before they existed.
    """
    raw = pd.Timestamp(api_value)
    return raw.tz_convert("UTC") if raw.tzinfo else raw.tz_localize("UTC")


def _sec_client(user_agent: str | None = None) -> httpx.Client:
    ua = user_agent or os.environ.get("SEC_USER_AGENT") or DEFAULT_SEC_UA
    return httpx.Client(timeout=30, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})


def _conservative_available_at(accepted: pd.Timestamp, filing_date: str) -> pd.Timestamp:
    accepted_et_date = accepted.tz_convert(ET).date().isoformat()
    if accepted_et_date == filing_date:
        return accepted
    disseminated = datetime.fromisoformat(filing_date).replace(hour=6, minute=0, tzinfo=ET)
    return pd.Timestamp(disseminated).tz_convert("UTC")


def _title_for(form: str, items: str, desc: str) -> str:
    if form.startswith("8-K") and items:
        names = [ITEM_8K.get(code.strip(), f"Item {code.strip()}") for code in items.split(",") if code.strip()]
        names = [n for n in names if not n.startswith("Financial Statements")] or names
        return f"{form}: " + "; ".join(names[:3])
    if desc:
        return f"{form}: {desc}"
    return form


def _filings_frame(block: dict, cik: int, symbol: str, forms: tuple[str, ...], start: pd.Timestamp, end: pd.Timestamp, retrieved_at: str) -> pd.DataFrame:
    n = len(block.get("accessionNumber", []))
    rows = []
    for i in range(n):
        form = block["form"][i]
        if form not in forms:
            continue
        acc_dt = block["acceptanceDateTime"][i]
        if not acc_dt:
            continue
        accepted = sec_acceptance_to_utc(acc_dt)
        if accepted < start or accepted > end:
            continue
        filing_date = block["filingDate"][i]
        acc = block["accessionNumber"][i]
        doc = block["primaryDocument"][i] or ""
        items = block.get("items", [""] * n)[i] or ""
        desc = block.get("primaryDocDescription", [""] * n)[i] or ""
        rows.append(
            {
                "event_id": f"sec:{acc}",
                "symbol": symbol,
                "provider": "sec_edgar",
                "form": form,
                "title": _title_for(form, items, desc),
                "summary": desc,
                "source_url": SEC_ARCHIVE.format(cik=cik, acc=acc.replace("-", ""), doc=doc),
                "published_at": accepted,
                "available_at": _conservative_available_at(accepted, filing_date),
                "retrieved_at": retrieved_at,
                "availability_basis": "sec_acceptance",
                "availability_verified": True,
                "content_version": acc,
                "filing_date": filing_date,
                "items": items,
                "acceptance_api_raw": acc_dt,
            }
        )
    return pd.DataFrame(rows, columns=NEWS_COLUMNS)


def fetch_sec_filings(symbol: str, cik: int, start: str, end_inclusive: str, forms: tuple[str, ...] = DEFAULT_FORMS, client: httpx.Client | None = None) -> pd.DataFrame:
    own = client is None
    client = client or _sec_client()
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end_inclusive, tz="UTC") + pd.Timedelta(days=1)
    retrieved_at = datetime.now(timezone.utc).isoformat()
    try:
        resp = client.get(SEC_SUBMISSIONS.format(name=f"CIK{cik:010d}.json"))
        resp.raise_for_status()
        sub = resp.json()
        frames = [_filings_frame(sub["filings"]["recent"], cik, symbol, forms, start_ts, end_ts, retrieved_at)]
        for extra in sub["filings"].get("files", []):
            if pd.Timestamp(extra["filingTo"]) < start_ts.tz_localize(None) or pd.Timestamp(extra["filingFrom"]) > end_ts.tz_localize(None):
                continue
            time.sleep(0.15)
            more = client.get(SEC_SUBMISSIONS.format(name=extra["name"]))
            more.raise_for_status()
            frames.append(_filings_frame(more.json(), cik, symbol, forms, start_ts, end_ts, retrieved_at))
    finally:
        if own:
            client.close()
    frame = pd.concat(frames, ignore_index=True)
    return frame.drop_duplicates(subset=["event_id"]).sort_values("available_at").reset_index(drop=True)


def _month_chunks(start: str, end_inclusive: str):
    cur = pd.Timestamp(start)
    stop = pd.Timestamp(end_inclusive)
    while cur <= stop:
        nxt = (cur + pd.offsets.MonthBegin(1)).normalize()
        yield cur, min(nxt - pd.Timedelta(seconds=1), stop + pd.Timedelta(hours=23, minutes=59, seconds=59))
        cur = nxt


def fetch_alpha_vantage_news(symbol: str, start: str, end_inclusive: str, api_key: str) -> pd.DataFrame:
    """Alpha Vantage NEWS_SENTIMENT. Free keys allow ~25 requests/day; one request per month-chunk."""
    rows = []
    retrieved_at = datetime.now(timezone.utc).isoformat()
    with httpx.Client(timeout=60) as client:
        for lo, hi in _month_chunks(start, end_inclusive):
            params = {
                "function": "NEWS_SENTIMENT", "tickers": symbol, "limit": 1000, "sort": "EARLIEST",
                "time_from": lo.strftime("%Y%m%dT%H%M"), "time_to": hi.strftime("%Y%m%dT%H%M"), "apikey": api_key,
            }
            payload = client.get("https://www.alphavantage.co/query", params=params).json()
            if "feed" not in payload:
                print(f"  alpha_vantage {symbol} {lo.date()}: {payload.get('Information') or payload.get('Note') or payload}")
                break
            for item in payload["feed"]:
                published = pd.Timestamp(datetime.strptime(item["time_published"], "%Y%m%dT%H%M%S"), tz="UTC")
                rows.append({
                    "event_id": f"av:{symbol}:{item['time_published']}:{abs(hash(item['url'])) % 10**8}",
                    "symbol": symbol, "provider": "alpha_vantage", "form": "news",
                    "title": item.get("title", ""), "summary": item.get("summary", ""), "source_url": item.get("url", ""),
                    "published_at": published, "available_at": published, "retrieved_at": retrieved_at,
                    "availability_basis": "provider_timestamp", "availability_verified": False,
                    "content_version": item.get("url", ""), "filing_date": published.date().isoformat(), "items": item.get("source", ""),
                    "acceptance_api_raw": item["time_published"],
                })
            time.sleep(1.0)
    return pd.DataFrame(rows, columns=NEWS_COLUMNS)


def fetch_finnhub_news(symbol: str, start: str, end_inclusive: str, api_key: str) -> pd.DataFrame:
    """Finnhub company-news. Free keys cover roughly the trailing year."""
    rows = []
    retrieved_at = datetime.now(timezone.utc).isoformat()
    with httpx.Client(timeout=60) as client:
        for lo, hi in _month_chunks(start, end_inclusive):
            params = {"symbol": symbol, "from": lo.strftime("%Y-%m-%d"), "to": hi.strftime("%Y-%m-%d"), "token": api_key}
            resp = client.get("https://finnhub.io/api/v1/company-news", params=params)
            if resp.status_code != 200:
                print(f"  finnhub {symbol} {lo.date()}: HTTP {resp.status_code}")
                break
            for item in resp.json():
                published = pd.Timestamp(item["datetime"], unit="s", tz="UTC")
                rows.append({
                    "event_id": f"fh:{item['id']}", "symbol": symbol, "provider": "finnhub", "form": "news",
                    "title": item.get("headline", ""), "summary": item.get("summary", ""), "source_url": item.get("url", ""),
                    "published_at": published, "available_at": published, "retrieved_at": retrieved_at,
                    "availability_basis": "provider_timestamp", "availability_verified": False,
                    "content_version": str(item["id"]), "filing_date": published.date().isoformat(), "items": item.get("source", ""),
                    "acceptance_api_raw": str(item["datetime"]),
                })
            time.sleep(1.1)
    return pd.DataFrame(rows, columns=NEWS_COLUMNS)


def fetch_universe_news(universe: list[dict], start: str, end_inclusive: str) -> pd.DataFrame:
    frames = []
    with _sec_client() as client:
        for entry in universe:
            frame = fetch_sec_filings(entry["ticker"], int(entry["cik"]), start, end_inclusive, client=client)
            print(f"  {entry['ticker']:6} sec_edgar {len(frame):5} filings")
            frames.append(frame)
            time.sleep(0.2)

    av_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    fh_key = os.environ.get("FINNHUB_API_KEY")
    for entry in universe:
        if av_key:
            frame = fetch_alpha_vantage_news(entry["ticker"], start, end_inclusive, av_key)
            print(f"  {entry['ticker']:6} alpha_vantage {len(frame):5} articles")
            frames.append(frame)
        if fh_key:
            frame = fetch_finnhub_news(entry["ticker"], start, end_inclusive, fh_key)
            print(f"  {entry['ticker']:6} finnhub {len(frame):5} articles")
            frames.append(frame)
    if not av_key and not fh_key:
        print("  (no ALPHAVANTAGE_API_KEY / FINNHUB_API_KEY set; news snapshot is SEC EDGAR only)")

    news = pd.concat(frames, ignore_index=True)
    news["published_at"] = pd.to_datetime(news["published_at"], utc=True)
    news["available_at"] = pd.to_datetime(news["available_at"], utc=True)
    return news.drop_duplicates(subset=["event_id"]).sort_values(["available_at", "event_id"]).reset_index(drop=True)
