"""Fixed price history for the memory arm.

The model-written SQL in the last window returned the wrong symbol and, twice,
a syntax error. This recall asks for the last 21 completed daily closes of each
trial symbol and states the change to the latest close. The adapter still
applies as_of, so the brief cannot see a later bar.
"""
from __future__ import annotations

import re

from meterless.query import AnalysisSource, QueryRequest, QueryResult

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_LOOKBACK = 21


def _sql(symbol: str) -> str:
    return (
        "SELECT ts, close FROM bars WHERE interval = 'daily' "
        f"AND symbol = '{symbol}' ORDER BY ts DESC LIMIT {_LOOKBACK}"
    )


def _line(symbol: str, rows: list[dict]) -> str:
    if len(rows) < 2:
        return f"{symbol}: fewer than two completed closes"
    latest = rows[0]
    last_close = float(latest["close"])
    parts = [f"{symbol} latest {str(latest['ts'])[:10]} {last_close:.2f}"]
    marks = []
    if len(rows) > 5:
        marks.append((5, rows[5]))
    marks.append((len(rows) - 1, rows[-1]))
    for sessions, older in marks:
        close = float(older["close"])
        change = (last_close / close - 1) * 100 if close else 0.0
        parts.append(f"{sessions} sessions earlier {str(older['ts'])[:10]} {close:.2f} ({change:+.2f}%)")
    return "; ".join(parts)


def fetch_brief(source: AnalysisSource, request: dict) -> tuple[str, list[QueryResult]]:
    """One correct query per symbol. Returns prompt text and the query records."""
    symbols = (request.get("data") or {}).get("symbols") or []
    found: list[QueryResult] = []
    lines = [
        "Retrieved history, already cut off at as_of. A positive percent means the latest close is higher than that earlier close.",
    ]
    for symbol in symbols:
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            continue
        result = source.execute(QueryRequest(
            run_id=str(request.get("run_id") or ""),
            request_id=str(request.get("request_id") or ""),
            query_id=f"{request.get('request_id')}-brief-{symbol}",
            goal=f"last {_LOOKBACK} daily closes for {symbol}",
            sql=_sql(symbol),
            as_of=str(request.get("as_of") or ""),
        ))
        found.append(result)
        lines.append(_line(symbol, [] if result.error else result.rows))
    return "\n".join(lines), found
