"""Shared JDBC plumbing for the Zetaris scripts (publish, probe). Simulator side only.

Settings come from the environment, or from the repository-root `.env` when python-dotenv
is installed. Passwords are only passed to the driver; they are never printed.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FEED_TABLE_ENVS = {
    "prices_hourly": "ZETARIS_FEED_PRICES_HOURLY",
    "prices_daily": "ZETARIS_FEED_PRICES_DAILY",
    "news": "ZETARIS_FEED_NEWS",
    "manifest": "ZETARIS_FEED_MANIFEST",
}

IDENTITIES = {
    "sim": ("ZETARIS_SIM_USER", "ZETARIS_SIM_PASSWORD"),
    "agent": ("ZETARIS_AGENT_USER", "ZETARIS_AGENT_PASSWORD"),
}


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO_ROOT / ".env")


def require(names: list[str]) -> dict[str, str]:
    values = {n: os.environ.get(n) or "" for n in names}
    missing = [n for n, v in values.items() if not v]
    if missing:
        raise SystemExit(f"missing settings (see .env.example): {', '.join(missing)}")
    return values


def feed_tables() -> dict[str, str]:
    values = require(list(FEED_TABLE_ENVS.values()))
    return {key: values[env] for key, env in FEED_TABLE_ENVS.items()}


def connect(identity: str):
    """Open a JDBC connection as the `sim` or `agent` identity."""
    import jaydebeapi  # lazy: only the Zetaris scripts need Java

    user_env, password_env = IDENTITIES[identity]
    cfg = require(["ZETARIS_JDBC_URL", "ZETARIS_JDBC_DRIVER", "ZETARIS_JDBC_JAR", user_env])
    return jaydebeapi.connect(
        cfg["ZETARIS_JDBC_DRIVER"],
        cfg["ZETARIS_JDBC_URL"],
        [cfg[user_env], os.environ.get(password_env) or ""],
        cfg["ZETARIS_JDBC_JAR"],
    )


def execute(conn, sql: str, params: list | None = None) -> list[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(sql, params or [])
        return cur.fetchall() if cur.description else []
    finally:
        cur.close()
