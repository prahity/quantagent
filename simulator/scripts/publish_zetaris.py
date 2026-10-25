"""Publish the frozen snapshot into the Zetaris released-feed tables (simulator identity).

    python scripts/publish_zetaris.py --create         # create tables if missing, then load
    python scripts/publish_zetaris.py                  # load (skips if this data_version is there)
    python scripts/publish_zetaris.py --replace        # delete every row, then load
    python scripts/publish_zetaris.py --fixtures       # load the small test slice instead
    python scripts/publish_zetaris.py --dry-run        # build rows and print counts, no JDBC

Each row carries `_released_epoch`; the agent's query tool only exposes rows released by
the decision's as_of (see sim/publish.py). The tables hold one data_version at a time, so
loading a different snapshot requires --replace.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

import _zetaris  # noqa: E402
from sim.publish import NEWS_COLUMNS, PRICE_COLUMNS, HIDDEN_COLUMNS, manifest_ddl, news_rows, price_rows, table_ddl  # noqa: E402
from sim.store import SnapshotStore  # noqa: E402


def _store(fixtures: bool) -> SnapshotStore:
    if not fixtures:
        return SnapshotStore.load()
    fx = ROOT / "tests" / "fixtures"
    bars = {i: pd.read_parquet(fx / f"bars_{i}.parquet") for i in ("hourly", "daily")}
    return SnapshotStore(bars, pd.read_parquet(fx / "news.parquet"), data_version="fixture-2025-01-27..2025-02-07")


def _literal(value) -> str:
    """Inline SQL literal for Spark (backslash escapes), for drivers without bind params."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def insert_rows(conn, table: str, columns: list[str], rows: list[dict], batch: int, inline: bool) -> None:
    cols = columns + HIDDEN_COLUMNS
    head = f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        if inline:
            values = ", ".join("(" + ", ".join(_literal(r[c]) for c in cols) + ")" for r in chunk)
            _zetaris.execute(conn, head + values)
        else:
            row_ph = "(" + ", ".join(["?"] * len(cols)) + ")"
            params = [r[c] for r in chunk for c in cols]
            _zetaris.execute(conn, head + ", ".join([row_ph] * len(chunk)), params)
        print(f"  {table}: {min(i + batch, len(rows))}/{len(rows)}", end="\r", flush=True)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--create", action="store_true", help="CREATE TABLE IF NOT EXISTS before loading")
    parser.add_argument("--using", default="iceberg", help="table format for --create ('' to omit USING)")
    parser.add_argument("--replace", action="store_true", help="delete all rows first (required to change data_version)")
    parser.add_argument("--fixtures", action="store_true", help="publish the test fixture slice")
    parser.add_argument("--batch", type=int, default=200, help="rows per INSERT statement")
    parser.add_argument("--inline", action="store_true", help="inline literals instead of ? parameters")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    store = _store(args.fixtures)
    data = {
        "prices_hourly": (PRICE_COLUMNS, price_rows(store, "hourly")),
        "prices_daily": (PRICE_COLUMNS, price_rows(store, "daily")),
        "news": (NEWS_COLUMNS, news_rows(store)),
    }
    counts = {key: len(rows) for key, (_, rows) in data.items()}
    print(f"data_version {store.data_version}: {counts}")
    if args.dry_run:
        return

    _zetaris.load_env()
    tables = _zetaris.feed_tables()
    conn = _zetaris.connect("sim")
    try:
        using = args.using or None
        if args.create:
            for key, (columns, _) in data.items():
                _zetaris.execute(conn, table_ddl(tables[key], columns, using))
            _zetaris.execute(conn, manifest_ddl(tables["manifest"], using))

        loaded = {str(r[0]) for r in _zetaris.execute(conn, f"SELECT data_version FROM {tables['manifest']}")}
        if loaded == {store.data_version} and not args.replace:
            print("already published; nothing to do (use --replace to reload)")
            return
        if loaded and not args.replace:
            raise SystemExit(f"tables hold {sorted(loaded)}; rerun with --replace to load {store.data_version}")
        if args.replace:
            for key in (*data, "manifest"):
                _zetaris.execute(conn, f"DELETE FROM {tables[key]}")

        for key, (columns, rows) in data.items():
            insert_rows(conn, tables[key], columns, rows, args.batch, args.inline)

        for key in data:
            got = int(_zetaris.execute(conn, f"SELECT COUNT(*) FROM {tables[key]}")[0][0])
            if got != counts[key]:
                raise SystemExit(f"{tables[key]}: expected {counts[key]} rows, found {got}")

        manifest_row = [store.data_version, datetime.now(timezone.utc).isoformat(),
                        counts["prices_hourly"], counts["prices_daily"], counts["news"]]
        if args.inline:
            values = ", ".join(_literal(v) for v in manifest_row)
            _zetaris.execute(conn, f"INSERT INTO {tables['manifest']} VALUES ({values})")
        else:
            _zetaris.execute(conn, f"INSERT INTO {tables['manifest']} VALUES (?, ?, ?, ?, ?)", manifest_row)
        print(f"published {store.data_version}; row counts verified")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
