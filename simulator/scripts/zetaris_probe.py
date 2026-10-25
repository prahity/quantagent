"""Check the Zetaris instance does what the feed and the query tool rely on.

    python scripts/zetaris_probe.py                 # both identities
    python scripts/zetaris_probe.py --identity sim

Run after simulator/config/zetaris_setup.sql and before publishing. Prints PASS/FAIL per
check and exits non-zero on any failure. The scratch table it creates under the sim
identity sits next to the feed tables and is dropped again.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _zetaris  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, fn) -> object:
    try:
        detail = fn()
        print(f"PASS  {name}" + (f"  ({detail})" if detail not in (None, True) else ""))
        RESULTS.append(True)
        return detail
    except Exception as exc:
        print(f"FAIL  {name}  ({type(exc).__name__}: {str(exc).splitlines()[0][:200]})")
        RESULTS.append(False)
        return None


def expect_denied(name: str, fn) -> None:
    try:
        fn()
    except Exception as exc:
        print(f"PASS  {name}  (denied: {type(exc).__name__})")
        RESULTS.append(True)
        return
    print(f"FAIL  {name}  (statement succeeded; the agent identity must not be allowed this)")
    RESULTS.append(False)


def _sibling(table: str, name: str) -> str:
    """A table name in the same namespace as `table`."""
    head, _, _ = table.rpartition(".")
    return f"{head}.{name}" if head else name


def probe_sim(tables: dict[str, str]) -> None:
    conn = check("sim: connect", lambda: _zetaris.connect("sim"))
    if conn is None:
        return
    run = lambda sql, params=None: _zetaris.execute(conn, sql, params)  # noqa: E731
    try:
        check("sim: SELECT 1", lambda: _assert(run("SELECT 1")[0][0] == 1))
        check("sim: WITH ... , ... AS + ? params",
              lambda: _assert(run("WITH a AS (SELECT ? AS x), b AS (SELECT x FROM a) SELECT x FROM b", [5])[0][0] == 5))
        check("sim: BIGINT comparison with params",
              lambda: _assert(run("SELECT CASE WHEN CAST(? AS BIGINT) <= CAST(? AS BIGINT) THEN 1 ELSE 0 END",
                                  [1738075800, 1738079400])[0][0] == 1))
        check("sim: JDBC value types",
              lambda: ", ".join(type(v).__name__ for v in run(
                  "SELECT CAST('2025-01-28 14:30:00' AS TIMESTAMP), CAST(1.25 AS DECIMAL(10,2)), "
                  "CAST(7 AS BIGINT), CAST(1.5 AS DOUBLE), 'text'")[0]))

        scratch = _sibling(tables["news"], f"probe_{uuid.uuid4().hex[:8]}")
        created = check(f"sim: CREATE TABLE {scratch}",
                        lambda: run(f"CREATE TABLE {scratch} (k STRING, v BIGINT) USING iceberg") or True)
        if created:
            check("sim: INSERT with ? params (multi-row)",
                  lambda: run(f"INSERT INTO {scratch} (k, v) VALUES (?, ?), (?, ?)", ["a", 1, "b", 2]) or True)
            check("sim: read back", lambda: _assert(run(f"SELECT COUNT(*) FROM {scratch}")[0][0] == 2))
            check("sim: DELETE FROM", lambda: run(f"DELETE FROM {scratch}") or True)
            check("sim: DROP TABLE", lambda: run(f"DROP TABLE {scratch}") or True)
        for key, table in tables.items():
            check(f"sim: SELECT from {table}", lambda t=table: f"{run(f'SELECT COUNT(*) FROM {t}')[0][0]} rows")
    finally:
        conn.close()


def probe_agent(tables: dict[str, str]) -> None:
    conn = check("agent: connect", lambda: _zetaris.connect("agent"))
    if conn is None:
        return
    run = lambda sql, params=None: _zetaris.execute(conn, sql, params)  # noqa: E731
    try:
        for key, table in tables.items():
            check(f"agent: SELECT from {table}", lambda t=table: f"{run(f'SELECT COUNT(*) FROM {t}')[0][0]} rows")
        cols = "event_id, symbol, provider, form, title, summary, source_url, published_at, available_at, released_at, _released_epoch, _scope_epoch, data_version"
        expect_denied("agent: INSERT into the news feed",
                      lambda: run(f"INSERT INTO {tables['news']} ({cols}) VALUES ('probe', 'X', 'p', 'f', 't', 's', 'u', 'a', 'b', 'c', 0, 0, 'probe')"))
        # DELETE on an empty table is a no-op the engine lets through, so plant a sentinel row
        # (as the sim identity) and check the agent's DELETE neither succeeds nor removes it.
        sentinel = f"probe-{uuid.uuid4().hex[:8]}"
        manifest = tables["manifest"]
        sim = _zetaris.connect("sim")
        try:
            _zetaris.execute(sim, f"INSERT INTO {manifest} VALUES ('{sentinel}', 'probe', 0, 0, 0)")
            expect_denied("agent: DELETE from the feed manifest",
                          lambda: run(f"DELETE FROM {manifest} WHERE data_version = '{sentinel}'"))
            left = _zetaris.execute(sim, f"SELECT COUNT(*) FROM {manifest} WHERE data_version = '{sentinel}'")[0][0]
            check("agent: DELETE removed nothing", lambda: _assert(left == 1))
        finally:
            _zetaris.execute(sim, f"DELETE FROM {manifest} WHERE data_version = '{sentinel}'")
            sim.close()
        scratch = _sibling(tables["news"], f"probe_{uuid.uuid4().hex[:8]}")
        expect_denied("agent: CREATE TABLE", lambda: run(f"CREATE TABLE {scratch} (k STRING) USING iceberg"))
    finally:
        conn.close()


def _assert(ok: bool) -> bool:
    if not ok:
        raise AssertionError("unexpected result")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--identity", choices=["sim", "agent", "both"], default="both")
    args = parser.parse_args()

    _zetaris.load_env()
    tables = _zetaris.feed_tables()
    if args.identity in ("sim", "both"):
        probe_sim(tables)
    if args.identity in ("agent", "both"):
        probe_agent(tables)
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
