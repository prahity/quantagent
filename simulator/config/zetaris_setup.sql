-- Zetaris setup for the AI Trader released feed. Run once, in the SQL editor of the
-- local web UI (http://localhost:3000), signed in as admin@zetaris.com. The editor sends
-- its whole buffer as ONE statement, so select and run one statement at a time.
--
-- Syntax follows the Zetaris SQL guide (https://kbase.zetaris.com/knowledge/sql-guide),
-- which is marked "in revision": confirm each statement on your instance, then run
-- `python scripts/zetaris_probe.py` (from simulator/) to check the result.
--
-- Logins are sign-in emails. The simulator publishes as the platform admin
-- (ZETARIS_SIM_USER=admin@zetaris.com); this script only adds the read-only agent login.
-- Replace <agent password> and put the same value in .env as ZETARIS_AGENT_PASSWORD.
--
-- The fully qualified table names below go into .env as ZETARIS_FEED_PRICES_HOURLY,
-- ZETARIS_FEED_PRICES_DAILY, ZETARIS_FEED_NEWS and ZETARIS_FEED_MANIFEST.

-- 1. Writable storage. The guide documents writes (CREATE TABLE ... USING iceberg, INSERT)
--    for lakehouses. If your instance only allows writes through a registered database
--    (e.g. Postgres), create the tables there instead and pass --using '' to the publisher.
--    storagePath is a path INSIDE the driver container, not on the Mac: the platform's
--    `customer-data` volume, whose file-system root is /home/zetaris/customer-data/shared.
--    The folder must already exist ("Path does not exist or is not allowed" otherwise):
--      docker exec zetaris-driver-1 mkdir -p /home/zetaris/customer-data/shared/aitrader-lake
--    Lightning DDL takes OPTIONS with double-quoted values (not WITH), as in the
--    platform's own bin/seed-org.sql. Verified on the local 2.4.3 install.
CREATE DATALAKE aitrader_lake OPTIONS (storageType "Local", storagePath "/home/zetaris/customer-data/shared/aitrader-lake");

-- 2. Feed tables. Agent-facing columns match FEED_TABLES in agent/config.py; the last three
--    are hidden (release time, lookback column, snapshot version). Timestamps are ISO-8601
--    strings so rows are identical to the local SQLite feed. The publisher can also create
--    these itself: python scripts/publish_zetaris.py --create
CREATE TABLE IF NOT EXISTS aitrader_lake.feed.feed_prices_hourly (
  symbol STRING, ts STRING, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,
  bar_end STRING, released_at STRING,
  _released_epoch BIGINT, _scope_epoch BIGINT, data_version STRING
) USING iceberg;

CREATE TABLE IF NOT EXISTS aitrader_lake.feed.feed_prices_daily (
  symbol STRING, ts STRING, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,
  bar_end STRING, released_at STRING,
  _released_epoch BIGINT, _scope_epoch BIGINT, data_version STRING
) USING iceberg;

CREATE TABLE IF NOT EXISTS aitrader_lake.feed.feed_news (
  event_id STRING, symbol STRING, provider STRING, form STRING, title STRING, summary STRING,
  source_url STRING, published_at STRING, available_at STRING, released_at STRING,
  _released_epoch BIGINT, _scope_epoch BIGINT, data_version STRING
) USING iceberg;

CREATE TABLE IF NOT EXISTS aitrader_lake.feed.feed_manifest (
  data_version STRING, loaded_at STRING,
  prices_hourly_rows BIGINT, prices_daily_rows BIGINT, news_rows BIGINT
) USING iceberg;

-- 3. Agent login. `general` level: it may not administer connections or users. The
--    simulator needs no login of its own here; it publishes as admin@zetaris.com.
ADD USER WITH (email 'aitrader-agent@zetaris.local', name 'aitrader_agent', level 'general', password '<agent password>');

-- Role and user names are quoted strings. The description allows only letters, digits
-- and _ . - , (no spaces or colons). Users are referenced by their sign-in email.
CREATE ROLE "aitrader_agent_role" DESCRIBE BY "ai-trader-agent,read-only-feed";
ASSIGN USER "aitrader-agent@zetaris.local" TO ROLE "aitrader_agent_role";

-- 4. Grants. The agent may only SELECT the feed: no other tables, no connectors, no INSERT.
--    Lakehouse tables are granted as DATALAKE TABLE. Verified on the local 2.4.3 install.
GRANT SELECT ON DATALAKE TABLE aitrader_lake.feed.feed_prices_hourly TO ROLE "aitrader_agent_role";
GRANT SELECT ON DATALAKE TABLE aitrader_lake.feed.feed_prices_daily TO ROLE "aitrader_agent_role";
GRANT SELECT ON DATALAKE TABLE aitrader_lake.feed.feed_news TO ROLE "aitrader_agent_role";
GRANT SELECT ON DATALAKE TABLE aitrader_lake.feed.feed_manifest TO ROLE "aitrader_agent_role";
