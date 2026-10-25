# Replicating the local Zetaris integration

Instructions for setting up a local Zetaris so this repo's agent can use it (`--backend zetaris`), written so that a Claude Code agent can follow them step by step. Every statement and expected output below was verified on Zetaris platform **2.4.3** (`zetaris-platform`, Docker Compose) on 2026-10-25.

**Starting point assumed:**
- `zetaris-platform` is installed and running per its own README (`docker compose up -d`, then `./verify.sh` passes).
- You can sign in at http://localhost:3000.
- This repo is cloned with the Zetaris integration code present (`simulator/scripts/publish_zetaris.py` exists).

**End state:**
- a lake `aitrader_lake` holding four feed tables loaded with the test slice;
- a read-only agent login;
- the probe and the live tests passing;
- a momentum run giving identical results on Zetaris and on local SQLite.

---

## 0. Facts that are easy to get wrong

| Fact | Detail |
|---|---|
| **Port 3000 is only the web UI.** | Code connects over JDBC to the query engine on **`localhost:10000`** (`zetaris-driver-1`). Both are the same Zetaris. Check with `docker ps`; the driver publishes `127.0.0.1:10000->10000`. If `DRIVER_PORT` is set in `zetaris-platform/.env`, use that port. |
| JDBC driver | `<zetaris-platform>/jdbc/ndp-jdbc-driver-<version>-driver.jar`, class `com.zetaris.lightning.jdbc.LightningDriver`, URL `jdbc:zetaris:lightning@localhost:10000` (no `//`). |
| Logins are **emails** | The admin is `admin@zetaris.com`. The password is `ZETARIS_ADMIN_PASSWORD` from `zetaris-platform/.env` (default `Zetaris1@`) unless it was changed in the UI. |
| The UI's SQL editor runs its **whole buffer as one statement** | Paste and run one statement at a time, or use the JDBC snippet in step 3. |
| Lightning DDL ≠ the online SQL guide | `CREATE DATALAKE … OPTIONS (k "v")`, not `WITH`. Role and user names are **quoted strings**. Grants use `ON DATALAKE TABLE`. Role descriptions allow only letters, digits and `_ . - ,` (no spaces or colons). |
| Lake storage path | A path **inside the driver container**, under the `customer-data` volume (`/home/zetaris/customer-data/shared/...`). It must already exist. |
| Password policy | 8+ characters, with a lower-case letter, an upper-case letter, a digit and one of `$ @ ! % * ? &`. |
| `SHOW DATABASES` is disabled | Use `SHOW DATALAKES`, `SHOW LIGHTNING DATABASES`, `SHOW TABLES IN aitrader_lake.feed`. |

---

## 1. Prerequisites (repo root)

```bash
java -version                    # needs a JDK 17+; macOS: brew install --cask temurin@17
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt       # includes jaydebeapi, jpype1, sqlglot
(cd simulator && uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -e ".[dev]")
```

Expected: `.venv/bin/python -c "import jaydebeapi, jpype, sqlglot"` exits 0. A `SyntaxWarning` from jaydebeapi is harmless.

## 2. Configure `.env` (repo root, gitignored)

```bash
cp .env.example .env
ls <zetaris-platform>/jdbc/        # note the exact jar filename
```

Set these values. The others in `.env.example` can stay as they are.

```dotenv
QUERY_BACKEND=sqlite
ZETARIS_JDBC_URL=jdbc:zetaris:lightning@localhost:10000
ZETARIS_JDBC_DRIVER=com.zetaris.lightning.jdbc.LightningDriver
ZETARIS_JDBC_JAR=/absolute/path/to/zetaris-platform/jdbc/ndp-jdbc-driver-<version>-driver.jar
ZETARIS_SIM_USER=admin@zetaris.com
ZETARIS_SIM_PASSWORD=<admin password>
ZETARIS_AGENT_USER=aitrader-agent@zetaris.local
ZETARIS_AGENT_PASSWORD=<choose one; must meet the password policy>
ZETARIS_FEED_PRICES_HOURLY=aitrader_lake.feed.feed_prices_hourly
ZETARIS_FEED_PRICES_DAILY=aitrader_lake.feed.feed_prices_daily
ZETARIS_FEED_NEWS=aitrader_lake.feed.feed_news
ZETARIS_FEED_MANIFEST=aitrader_lake.feed.feed_manifest
```

Keep `QUERY_BACKEND=sqlite`; pass `--backend zetaris` per run. Never print or commit the passwords.

Check connectivity as admin:

```bash
nc -z localhost 10000 && echo open
cd simulator && ../.venv/bin/python scripts/zetaris_probe.py --identity sim
```

Expected: `sim: connect`, `SELECT 1`, the parameter and dialect checks, and `CREATE/INSERT/DELETE/DROP` all show PASS. The four `SELECT from aitrader_lake.feed.*` lines **FAIL until step 4**. That's expected.

## 3. Create the lake

The lake's folder must exist inside the driver container first:

```bash
docker exec zetaris-driver-1 mkdir -p /home/zetaris/customer-data/shared/aitrader-lake
```

Then run the DDL as admin, either in the UI SQL editor or over JDBC with this snippet, run from `simulator/`. Reuse the snippet for steps 5–6 by changing `STATEMENTS`:

```bash
cd simulator && ../.venv/bin/python -W ignore - <<'EOF' 2>&1 | grep -v SLF4J
import sys; sys.path.insert(0, "scripts")
import _zetaris
_zetaris.load_env()
STATEMENTS = [
    'CREATE DATALAKE aitrader_lake OPTIONS (storageType "Local", storagePath "/home/zetaris/customer-data/shared/aitrader-lake")',
    "SHOW DATALAKES",
]
conn = _zetaris.connect("sim")      # admin identity from .env
for sql in STATEMENTS:
    try:
        print("OK ", sql[:80], "->", _zetaris.execute(conn, sql)[:5])
    except Exception as exc:
        print("ERR", sql[:80], "->", str(exc).splitlines()[0][-250:])
conn.close()
EOF
```

Expected: `SHOW DATALAKES -> [('aitrader_lake', 'From DataLakeWarehouse')]`.

## 4. Create the feed tables and load the test slice

The publisher creates the tables (`--create`), loads the real fixture slice (AAPL, MSFT and SPY, 2025-01-27 → 2025-02-07) and checks the row counts:

```bash
cd simulator && ../.venv/bin/python -W ignore scripts/publish_zetaris.py --create --fixtures
```

Expected last lines:

```
data_version fixture-2025-01-27..2025-02-07: {'prices_hourly': 210, 'prices_daily': 30, 'news': 5}
published fixture-2025-01-27..2025-02-07; row counts verified
```

If the driver rejects `?` parameters, rerun with `--inline`. To switch datasets later, use `--replace`; the tables hold one `data_version` at a time.

## 5. Create the read-only agent login

Run as admin, in the UI or with the step 3 snippet. Replace `<agent password>` with the value of `ZETARIS_AGENT_PASSWORD`. Run these exact forms:

```sql
ADD USER WITH (email 'aitrader-agent@zetaris.local', name 'aitrader_agent', level 'general', password '<agent password>')
CREATE ROLE "aitrader_agent_role" DESCRIBE BY "ai-trader-agent,read-only-feed"
ASSIGN USER "aitrader-agent@zetaris.local" TO ROLE "aitrader_agent_role"
GRANT SELECT ON DATALAKE TABLE aitrader_lake.feed.feed_prices_hourly TO ROLE "aitrader_agent_role"
GRANT SELECT ON DATALAKE TABLE aitrader_lake.feed.feed_prices_daily TO ROLE "aitrader_agent_role"
GRANT SELECT ON DATALAKE TABLE aitrader_lake.feed.feed_news TO ROLE "aitrader_agent_role"
GRANT SELECT ON DATALAKE TABLE aitrader_lake.feed.feed_manifest TO ROLE "aitrader_agent_role"
```

Check with `SHOW USERS` and `SHOW USER ASSIGNED TO ROLE "aitrader_agent_role"`. Both should list `AITRADER-AGENT@ZETARIS.LOCAL`.

Don't write the real password into `simulator/config/zetaris_setup.sql`, which is committed. If the password in `.env` turns out wrong, reset it as admin with `RESET USER PASSWORD "aitrader-agent@zetaris.local" "<new password>"` (two quoted strings; the policy above applies).

## 6. Verify

```bash
cd simulator && ../.venv/bin/python -W ignore scripts/zetaris_probe.py      # both identities
cd .. && ZETARIS_LIVE=1 .venv/bin/python -W ignore -m unittest tests.test_zetaris_live -v
.venv/bin/python -m unittest discover tests
```

Expected:
- **Probe:** all PASS, 23 checks. The agent login can SELECT the four tables, and its INSERT, DELETE and CREATE are each denied. `agent: DELETE removed nothing` passes.
- **Live tests:** 3/3 OK, taking about a minute:
  - `test_cutoff_hides_future_rows`;
  - `test_raw_table_is_refused_before_reaching_zetaris`;
  - `test_momentum_matches_the_local_backend`.
- **Unit tests:** OK (3 skipped without `ZETARIS_LIVE`).

## 7. Manual confirmation (what a human can look at)

**A. In the UI** (http://localhost:3000 → SQL editor, one statement at a time):

| Query | Expected |
|---|---|
| `SELECT * FROM aitrader_lake.feed.feed_manifest` | one row: `fixture-2025-01-27..2025-02-07`, 210, 30, 5 |
| `SELECT symbol, COUNT(*), MIN(ts), MAX(ts) FROM aitrader_lake.feed.feed_prices_hourly GROUP BY symbol ORDER BY symbol` | AAPL, MSFT and SPY, 70 each, `2025-01-27T14:30:00+00:00` → `2025-02-07T20:30:00+00:00` |
| `SELECT ts, close, released_at FROM aitrader_lake.feed.feed_prices_hourly WHERE symbol = 'AAPL' ORDER BY ts LIMIT 3` | closes 227.29…, 228.49…, 230.09…, each with `released_at` = `ts` + 1h |
| `SELECT symbol, form, title, released_at FROM aitrader_lake.feed.feed_news ORDER BY released_at` | 5 SEC filings: 3 MSFT (from 2025-01-22), 2 AAPL (2025-01-30, 2025-01-31) |
| `SELECT symbol, COUNT(*) FROM aitrader_lake.feed.feed_prices_hourly WHERE _released_epoch <= 1738081800 GROUP BY symbol` | 9 per symbol. This is what the agent may see at 2025-01-28 16:30 UTC. |

**B. Watch an agent run hit Zetaris:**
1. Open http://localhost:4040, the Spark UI, at the **SQL / DataFrame** tab.
2. Run:
   ```bash
   .venv/bin/python runner.py --agent momentum --data fixtures --backend zetaris --minutes 5
   .venv/bin/python runner.py --agent momentum --data fixtures --backend sqlite  --minutes 5
   ```
3. During the first run, new queries appear in the Spark UI, starting with `WITH prices AS (SELECT … FROM aitrader_lake.feed.feed_prices_hourly WHERE _released_epoch <= …`.
4. The two runs print identical per-step order lines, `fills` and `net_profit`.
5. `runs/<run_id>/query_log.jsonl` shows each query's `goal`, `sql`, `cutoff` and `row_count`.

**C. Tamper test (optional; proves the agent's decisions come from Zetaris):**
1. As admin, run `DELETE FROM aitrader_lake.feed.feed_prices_hourly WHERE symbol = 'AAPL'`.
2. Rerun both B commands. The results now differ, because the agent can't see AAPL on Zetaris while the simulator still scores from its own data.
3. Restore with `cd simulator && ../.venv/bin/python scripts/publish_zetaris.py --fixtures --replace`, then rerun. The results match again.

---

## Troubleshooting (errors actually seen)

| Error | Cause → fix |
|---|---|
| `Could not open client transport … localhost:3000` | Wrong port. Use `10000`; 3000 is the web UI. |
| `mismatched input 'with' expecting 'OPTIONS'` | `CREATE DATALAKE … WITH (…)`. Use `OPTIONS (storageType "Local", storagePath "…")`. |
| `== SQL ==` shows the whole file, comments included | The UI ran the entire buffer as one statement. Run one statement at a time. |
| `Path does not exist or is not allowed` | The lake folder doesn't exist in the container. Run the `docker exec … mkdir -p` from step 3. |
| `mismatched input 'aitrader_agent_role' expecting STRING` | Role and user names must be quoted: `CREATE ROLE "aitrader_agent_role" …`. |
| `Description is invalid, it must be alphanumeric …` | The role description has spaces or colons. Use e.g. `"ai-trader-agent,read-only-feed"`. |
| `mismatched input 'table' expecting {'ALL', …, 'DATALAKE', …}` | Grant syntax. Use `GRANT SELECT ON DATALAKE TABLE <lake>.<ns>.<table> TO ROLE "<role>"`. |
| `missing {'NAMESPACE', 'TABLE'}` | Same fix as the row above: add `TABLE` after `DATALAKE`. |
| `Peer indicated failure: Error validating the login` | The user doesn't exist, or the password in `.env` doesn't match. Check `SHOW USERS`, then fix `.env` or run `RESET USER PASSWORD`. |
| `SHOW NAMESPACES / SHOW TABLES are disabled` | Use `SHOW TABLES IN aitrader_lake.feed`. |
| Runner: `Zetaris feed does not hold data_version …` | The published dataset differs from `--data`. Fixtures need `--data fixtures`; for the full snapshot, run `fetch_data.py`, then `publish_zetaris.py --replace`, and use `--data snapshot`. |

## Next: the full dataset

```bash
cd simulator
SEC_USER_AGENT="Your Name you@example.com" ../.venv/bin/python scripts/fetch_data.py
../.venv/bin/python scripts/publish_zetaris.py --replace
```

Afterwards, run with `--data snapshot` and a date range, e.g. `--start 2025-03-03 --end 2025-03-14`.
