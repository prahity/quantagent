"""Strategy specs and the feature view the rule runner reads.

The feature SELECT names only `bars` and `filings`. The analysis source wraps
that statement in the as_of cutoff, so a lookback cannot see a later bar or filing.
"""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from meterless.query import RELATION_COLUMNS, AnalysisSource, QueryRequest

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOOKBACKS = (5, 21, 63)


class Signal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["momentum", "mean_reversion", "volatility", "relative_strength", "filing_event"] = "momentum"
    lookback: Literal[5, 21, 63] = 21
    max_days_since_filing: int | None = Field(default=None, ge=0, le=365)
    forms: list[str] = Field(default_factory=list)

    @field_validator("lookback", mode="before")
    @classmethod
    def _lookback(cls, value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 21
        return min(_LOOKBACKS, key=lambda item: abs(item - number))

    @field_validator("forms", mode="before")
    @classmethod
    def _forms(cls, value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            value = [value]
        return [str(item).upper()[:12] for item in value if str(item).strip()][:6]


class Selection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["top_n", "threshold"] = "top_n"
    top_n: int = Field(default=2, ge=1, le=8)
    threshold: float | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: Any) -> str:
        text = str(value or "top_n").lower().replace("-", "_")
        return text if text in ("top_n", "threshold") else "top_n"


class StrategySpec(BaseModel):
    """One deterministic rule. Weights are fractions, and each name is capped at 40%."""

    model_config = ConfigDict(extra="ignore")

    spec_id: str = ""
    parent_id: str | None = None
    name: str = "unnamed"
    universe: list[str] = Field(default_factory=list)
    signal: Signal = Field(default_factory=Signal)
    selection: Selection = Field(default_factory=Selection)
    target_weight: float = Field(default=0.30, ge=0, le=0.40)
    cash_floor: float = Field(default=0.10, ge=0, le=0.95)
    rebalance_every: int = Field(default=5, ge=1, le=63)
    min_weight_change: float = Field(default=0.05, ge=0, le=1)
    drawdown_cut: float = Field(default=0.12, ge=0, le=0.95)
    origin: Literal["model", "fallback"] = "model"

    @field_validator("target_weight", "cash_floor", "min_weight_change", "drawdown_cut", mode="before")
    @classmethod
    def _unit(cls, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        if number > 1:
            number = number / 100.0
        return number

    @field_validator("universe", mode="before")
    @classmethod
    def _universe(cls, value: Any) -> list[str]:
        if value in (None, "", "all"):
            return []
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",")]
        out = []
        for item in value:
            symbol = str(item).upper().strip()
            if _SYMBOL.fullmatch(symbol) and symbol not in out:
                out.append(symbol)
        return out[:12]

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> str:
        text = " ".join(str(value or "unnamed").split())
        return (text or "unnamed")[:80]


def parse_specs(payload: Any, parent_id: str | None, symbols: list[str]) -> list[StrategySpec]:
    """Turn a model JSON object or list into specs. Invalid entries are dropped."""
    if isinstance(payload, dict):
        payload = payload.get("strategies") or payload.get("specs") or payload.get("rules") or []
    if not isinstance(payload, list):
        return []
    specs = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        try:
            spec = StrategySpec.model_validate(item)
        except Exception:
            continue
        if not spec.universe:
            spec.universe = list(symbols)
        else:
            spec.universe = [symbol for symbol in spec.universe if symbol in symbols] or list(symbols)
        spec.spec_id = f"s{index + 1:02d}"
        spec.parent_id = parent_id
        spec.origin = "model"
        specs.append(spec)
    return specs


def fallback_specs(symbols: list[str], count: int, parent_id: str | None) -> list[StrategySpec]:
    """Diverse rules used only to fill a round the model did not fill."""
    grid = [
        ("mom-21-top2", "momentum", 21, 2, 0.30, 0.10, 5, 0.05, 0.12),
        ("mom-5-top1", "momentum", 5, 1, 0.40, 0.20, 1, 0.02, 0.08),
        ("mom-63-top2", "momentum", 63, 2, 0.25, 0.15, 10, 0.05, 0.15),
        ("rev-5-top2", "mean_reversion", 5, 2, 0.25, 0.20, 5, 0.04, 0.10),
        ("rev-21-top1", "mean_reversion", 21, 1, 0.30, 0.25, 5, 0.05, 0.10),
        ("vol-low-top2", "volatility", 21, 2, 0.30, 0.15, 10, 0.05, 0.12),
        ("vol-low-top1", "volatility", 21, 1, 0.40, 0.20, 5, 0.03, 0.08),
        ("rs-21-top2", "relative_strength", 21, 2, 0.30, 0.10, 5, 0.05, 0.12),
        ("rs-63-top1", "relative_strength", 63, 1, 0.40, 0.15, 10, 0.05, 0.15),
        ("file-21-top2", "filing_event", 21, 2, 0.25, 0.20, 5, 0.04, 0.10),
        ("mom-21-top1-cash", "momentum", 21, 1, 0.20, 0.40, 5, 0.05, 0.08),
        ("rev-63-top2", "mean_reversion", 63, 2, 0.20, 0.30, 21, 0.08, 0.18),
    ]
    out = []
    for name, kind, lookback, top_n, weight, floor, every, change, cut in grid[:count]:
        out.append(StrategySpec(
            spec_id="",
            parent_id=parent_id,
            name=name,
            universe=list(symbols),
            signal=Signal(kind=kind, lookback=lookback, max_days_since_filing=10 if kind == "filing_event" else None),
            selection=Selection(mode="top_n", top_n=top_n),
            target_weight=weight,
            cash_floor=floor,
            rebalance_every=every,
            min_weight_change=change,
            drawdown_cut=cut,
            origin="fallback",
        ))
    return out


def _in_list(symbols: list[str]) -> str:
    names: list[str] = []
    for symbol in list(symbols) + ["SPY"]:
        if _SYMBOL.fullmatch(symbol) and symbol not in names:
            names.append(symbol)
    if not names:
        raise ValueError("feature view needs at least one symbol")
    return ",".join(f"'{symbol}'" for symbol in names)


def feature_sql(symbols: list[str], as_of: str) -> str:
    """One row per symbol from bars and filings already cut off at as_of.

    `days_since_filing` and `days_until_filing` are filled from the filing dates
    after the query returns. Those dates come only from filings the cutoff kept.
    """
    day = str(as_of)[:10]
    if not _DAY.fullmatch(day):
        raise ValueError(f"as_of is not a calendar day: {as_of!r}")
    names = _in_list(symbols)
    return f"""
WITH daily AS (
  SELECT symbol, close, high, row_number() OVER (PARTITION BY symbol ORDER BY ts) AS n
  FROM bars
  WHERE interval = 'daily' AND symbol IN ({names})
),
mx AS (
  SELECT symbol, max(n) AS n_last FROM daily GROUP BY symbol
),
rets AS (
  SELECT a.symbol AS symbol, (a.close / b.close - 1.0) AS ret
  FROM daily a
  JOIN daily b ON a.symbol = b.symbol AND a.n = b.n + 1
  JOIN mx m ON a.symbol = m.symbol
  WHERE b.close > 0 AND a.n > m.n_last - 21 AND a.n <= m.n_last
),
px AS (
  SELECT d.symbol AS symbol, d.close AS close, d.n AS n
  FROM daily d
),
vol AS (
  SELECT symbol, sqrt(CASE WHEN avg(ret * ret) - avg(ret) * avg(ret) > 0
       THEN avg(ret * ret) - avg(ret) * avg(ret) ELSE 0 END) AS vol_21
  FROM rets GROUP BY symbol
  HAVING count(*) >= 21
),
ma21 AS (
  SELECT d.symbol AS symbol, avg(d.close) AS ma
  FROM daily d JOIN mx m ON d.symbol = m.symbol
  WHERE d.n > m.n_last - 21 AND d.n <= m.n_last
  GROUP BY d.symbol
  HAVING count(*) >= 21
),
ma63 AS (
  SELECT d.symbol AS symbol, avg(d.close) AS ma
  FROM daily d JOIN mx m ON d.symbol = m.symbol
  WHERE d.n > m.n_last - 63 AND d.n <= m.n_last
  GROUP BY d.symbol
  HAVING count(*) >= 63
),
hi63 AS (
  SELECT d.symbol AS symbol, max(d.high) AS hi
  FROM daily d JOIN mx m ON d.symbol = m.symbol
  WHERE d.n > m.n_last - 63 AND d.n <= m.n_last
  GROUP BY d.symbol
  HAVING count(*) >= 63
),
last_file AS (
  SELECT symbol, max(substr(available_at, 1, 10)) AS last_filing
  FROM filings WHERE symbol IN ({names}) GROUP BY symbol
),
next_file AS (
  SELECT symbol, min(substr(published_at, 1, 10)) AS next_filing
  FROM filings
  WHERE symbol IN ({names}) AND substr(published_at, 1, 10) > '{day}'
  GROUP BY symbol
)
SELECT c.symbol AS symbol, c.close AS close,
       CASE WHEN p5.close > 0 THEN c.close / p5.close - 1 END AS ret_5,
       CASE WHEN p21.close > 0 THEN c.close / p21.close - 1 END AS ret_21,
       CASE WHEN p63.close > 0 THEN c.close / p63.close - 1 END AS ret_63,
       v.vol_21 AS vol_21,
       CASE WHEN m21.ma > 0 THEN c.close / m21.ma - 1 END AS ma_gap_21,
       CASE WHEN m63.ma > 0 THEN c.close / m63.ma - 1 END AS ma_gap_63,
       CASE WHEN h.hi > 0 THEN (h.hi - c.close) / h.hi END AS dd_63,
       fl.last_filing AS last_filing,
       fn.next_filing AS next_filing
FROM daily c
JOIN mx m ON c.symbol = m.symbol AND c.n = m.n_last
LEFT JOIN px p5 ON p5.symbol = c.symbol AND p5.n = m.n_last - 5
LEFT JOIN px p21 ON p21.symbol = c.symbol AND p21.n = m.n_last - 21
LEFT JOIN px p63 ON p63.symbol = c.symbol AND p63.n = m.n_last - 63
LEFT JOIN vol v ON v.symbol = c.symbol
LEFT JOIN ma21 m21 ON m21.symbol = c.symbol
LEFT JOIN ma63 m63 ON m63.symbol = c.symbol
LEFT JOIN hi63 h ON h.symbol = c.symbol
LEFT JOIN last_file fl ON fl.symbol = c.symbol
LEFT JOIN next_file fn ON fn.symbol = c.symbol
""".strip()



def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _day_text(value: Any) -> str:
    text = str(value or "")[:10]
    return text if _DAY.fullmatch(text) else ""


def _days_between(later: str, earlier: str) -> int | None:
    if not later or not earlier:
        return None
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def _finish(rows: list[dict], as_of: str) -> list[dict]:
    day = str(as_of)[:10]
    ready = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        item = {
            "symbol": symbol,
            "close": _num(row.get("close")),
            "ret_5": _num(row.get("ret_5")),
            "ret_21": _num(row.get("ret_21")),
            "ret_63": _num(row.get("ret_63")),
            "vol_21": _num(row.get("vol_21")),
            "ma_gap_21": _num(row.get("ma_gap_21")),
            "ma_gap_63": _num(row.get("ma_gap_63")),
            "dd_63": _num(row.get("dd_63")),
            "last_filing": _day_text(row.get("last_filing")),
            "next_filing": _day_text(row.get("next_filing")),
        }
        item["days_since_filing"] = _days_between(day, item["last_filing"])
        until = _days_between(item["next_filing"], day)
        item["days_until_filing"] = until if until is not None and until >= 0 else None
        ready.append(item)
    spy = next((item["ret_21"] for item in ready if item["symbol"] == "SPY"), None)
    for item in ready:
        item["rs_spy"] = None if item["ret_21"] is None or spy is None else item["ret_21"] - spy
    ready.sort(key=lambda item: item["symbol"])
    return ready


def _python_features(source: AnalysisSource, as_of: str, symbols: list[str]) -> list[dict]:
    """Same columns from released daily bars when the view SQL is rejected."""
    names = _in_list(symbols)
    bars = source.execute(QueryRequest(
        run_id="features", request_id="features", query_id=f"bars-{str(as_of)[:10]}",
        goal="released daily bars for the feature view",
        sql=f"SELECT symbol, ts, close, high FROM bars WHERE interval = 'daily' AND symbol IN ({names}) ORDER BY symbol, ts",
        as_of=str(as_of),
    ), row_limit=20000)
    filings = source.execute(QueryRequest(
        run_id="features", request_id="features", query_id=f"filings-{str(as_of)[:10]}",
        goal="released filings for the feature view",
        sql=f"SELECT symbol, substr(published_at, 1, 10) AS published_on, substr(available_at, 1, 10) AS available_on FROM filings WHERE symbol IN ({names})",
        as_of=str(as_of),
    ), row_limit=20000)
    if bars.error:
        raise RuntimeError(bars.error)
    by_symbol: dict[str, list[dict]] = {}
    for row in bars.rows:
        by_symbol.setdefault(str(row["symbol"]).upper(), []).append(row)
    last_filing: dict[str, str] = {}
    next_filing: dict[str, str] = {}
    day = str(as_of)[:10]
    for row in [] if filings.error else filings.rows:
        symbol = str(row.get("symbol") or "").upper()
        available = _day_text(row.get("available_on"))
        published = _day_text(row.get("published_on"))
        if available and (symbol not in last_filing or available > last_filing[symbol]):
            last_filing[symbol] = available
        if published and published > day and (symbol not in next_filing or published < next_filing[symbol]):
            next_filing[symbol] = published
    built = []
    for symbol, series in by_symbol.items():
        series.sort(key=lambda item: str(item["ts"]))
        closes = [_num(item.get("close")) for item in series]
        highs = [_num(item.get("high")) for item in series]
        if not closes or closes[-1] is None:
            continue

        def ret(lag: int) -> float | None:
            if len(closes) <= lag or not closes[-1 - lag]:
                return None
            return closes[-1] / closes[-1 - lag] - 1

        window = [value for value in closes[-22:] if value]
        rets = [window[i] / window[i - 1] - 1 for i in range(1, len(window)) if window[i - 1]]
        vol = None
        if len(rets) >= 21:
            mean = sum(rets) / len(rets)
            var = sum(item * item for item in rets) / len(rets) - mean * mean
            vol = math.sqrt(var) if var > 0 else 0.0
        def ma_gap(n: int) -> float | None:
            sample = [value for value in closes[-n:] if value]
            if len(sample) < n or not sum(sample):
                return None
            average = sum(sample) / len(sample)
            return None if average == 0 else closes[-1] / average - 1
        hi = [value for value in highs[-63:] if value]
        peak = max(hi) if len(hi) >= 63 else None
        built.append({
            "symbol": symbol,
            "close": closes[-1],
            "ret_5": ret(5),
            "ret_21": ret(21),
            "ret_63": ret(63),
            "vol_21": vol,
            "ma_gap_21": ma_gap(21),
            "ma_gap_63": ma_gap(63),
            "dd_63": None if not peak else (peak - closes[-1]) / peak,
            "last_filing": last_filing.get(symbol, ""),
            "next_filing": next_filing.get(symbol, ""),
        })
    return _finish(built, as_of)


def load_features(source: AnalysisSource, as_of: str, symbols: list[str]) -> list[dict]:
    """Run the feature view at `as_of`. The source enforces the cutoff."""
    result = source.execute(QueryRequest(
        run_id="features",
        request_id="features",
        query_id=f"features-{str(as_of)[:10]}",
        goal="feature view at as_of",
        sql=feature_sql(symbols, as_of),
        as_of=str(as_of),
    ))
    if result.error or not result.rows:
        if result.error:
            load_features.last_error = result.error
        return _python_features(source, as_of, symbols)
    load_features.last_error = ""
    return _finish(result.rows, as_of)


load_features.last_error = ""

_FEATURE_COLUMNS = RELATION_COLUMNS["features"]
_FEATURE_KIND = {
    "symbol": "STRING", "close": "DOUBLE", "ret_5": "DOUBLE", "ret_21": "DOUBLE", "ret_63": "DOUBLE",
    "vol_21": "DOUBLE", "ma_gap_21": "DOUBLE", "ma_gap_63": "DOUBLE", "dd_63": "DOUBLE", "rs_spy": "DOUBLE",
    "days_since_filing": "INT", "days_until_filing": "INT", "last_filing": "STRING", "next_filing": "STRING",
}


def _feature_literal(column: str, value: Any) -> str:
    kind = _FEATURE_KIND[column]
    if value is None or value == "":
        return f"CAST(NULL AS {kind})"
    if kind == "STRING":
        return "CAST('" + str(value).replace("'", "''") + f"' AS {kind})"
    if kind == "INT":
        return f"CAST({int(value)} AS {kind})"
    return f"CAST({float(value)} AS {kind})"


def bind_features(sql: str, rows: list[dict]) -> str:
    """Put the as_of feature rows in front of one agent statement as `features`."""
    if rows:
        selects = []
        for row in rows:
            columns = ", ".join(f"{_feature_literal(column, row.get(column))} AS {column}" for column in _FEATURE_COLUMNS)
            selects.append(f"SELECT {columns}")
        body = " UNION ALL ".join(selects)
    else:
        columns = ", ".join(f"CAST(NULL AS {_FEATURE_KIND[column]}) AS {column}" for column in _FEATURE_COLUMNS)
        body = f"SELECT {columns} WHERE 1 = 0"
    clause = f"features AS ({body})"
    statement = sql.strip().rstrip(";").strip()
    if statement.lower().startswith("with"):
        return f"WITH {clause}, {statement[4:].lstrip()}"
    return f"WITH {clause} {statement}"
