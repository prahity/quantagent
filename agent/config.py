"""Pinned agent configuration.

Everything that must stay identical across base / intermediate / final / ablation
agents lives here: model inference settings, the query tool's safety limits, and the
released-feed table descriptions the agent is told about. Pin these; do not read them
from the model's own output.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Where the agent reads the architecture from at run start. The canonical file lives at
# the repository root; a docs/ copy is used if present.
ARCHITECTURE_CANDIDATES = (
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "AI-Trader-Architecture.md",
)


def architecture_path() -> Path | None:
    for candidate in ARCHITECTURE_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


@dataclass(frozen=True)
class ModelConfig:
    """Pinned Nemotron inference settings. Values come from .env for identity only;
    temperature and max_tokens are fixed here so every agent decides the same way."""

    base_url_env: str = "NEMOTRON_BASE_URL"
    api_key_env: str = "NEMOTRON_API_KEY"
    model_id_env: str = "NEMOTRON_MODEL_ID"

    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 4096
    timeout_s: float = 60.0

    # Reasoning control. "off" is pinned by default because this hosted model otherwise
    # spends its whole token budget in a separate reasoning channel and returns empty
    # content (measured). Overridable per run via the THINKING env var (off|low|on):
    #   off -> chat_template_kwargs {"enable_thinking": false}  (the only mechanism the
    #          hosted endpoint actually honors; /no_think and "detailed thinking off" do not)
    #   low -> reasoning_effort "low"   (reduces but does not remove reasoning)
    #   on  -> no directive (full reasoning)
    thinking: str = "off"
    thinking_env: str = "THINKING"

    # The hosted endpoint rejects nvext.guided_json (HTTP 400: unknown field), so structured
    # output is enforced by prompt + Pydantic validation instead. Flip to True only on an
    # endpoint that accepts it; the schema is then sent as {"nvext": {"guided_json": ...}}.
    use_guided_json: bool = False

    # Retry policy for 429 / 5xx.
    max_retries: int = 4
    backoff_base_s: float = 1.5
    backoff_cap_s: float = 20.0


@dataclass(frozen=True)
class QueryConfig:
    """Safety envelope for the zetaris_query tool. The cutoff is server-bound per
    decision; the agent can never widen any of these."""

    backend_env: str = "QUERY_BACKEND"           # "sqlite" | "zetaris"
    row_limit: int = 500                          # max rows returned per call
    timeout_s: float = 5.0                        # per-query wall clock
    zetaris_timeout_s: float = 30.0               # remote engine: per-query wall clock
    calls_per_decision: int = 8                   # per-decision call budget


# Agent-facing released-feed tables. Each is exposed to SQL as a CTE already filtered
# by `released_at <= as_of`; the agent may only name these. Keep in sync with the
# columns the runner materialises from the simulator feed and with
# simulator/scripts/publish_zetaris.py, which loads the same columns into Zetaris.
#
# Every raw table also carries two hidden columns the agent never sees:
#   _released_epoch  release time (epoch s); the cutoff filter
#   _scope_epoch     the run-window column (epoch s) the lookback filter applies to
FEED_TABLES: dict[str, dict] = {
    "prices": {
        "columns": ["symbol", "ts", "open", "high", "low", "close", "volume", "bar_end", "released_at"],
        "released_col": "released_at",
        "scope_col": "ts",
        "description": (
            "Completed OHLCV bars released by the cutoff. One row per symbol per bar. "
            "`ts` is the bar start, `bar_end` its completion time, `released_at` when the "
            "simulator exposed it. A bar is only here once complete."
        ),
    },
    "news": {
        "columns": [
            "event_id", "symbol", "provider", "form", "title", "summary",
            "source_url", "published_at", "available_at", "released_at",
        ],
        "released_col": "released_at",
        "scope_col": "available_at",
        "description": (
            "Timestamped news / filing records released by the cutoff. `published_at`, "
            "`available_at` and `released_at` are all <= the decision cutoff. Titles are "
            "SEC item names for EDGAR filings."
        ),
    },
}


def table_descriptions() -> str:
    """Human-readable schema the policy puts in the prompt."""
    lines = []
    for name, spec in FEED_TABLES.items():
        cols = ", ".join(spec["columns"])
        lines.append(f"- {name}({cols})\n    {spec['description']}")
    return "\n".join(lines)


@dataclass(frozen=True)
class PolicyConfig:
    max_model_steps: int = 6          # query/decision turns per decision before forced hold
    memory_max_records: int = 12      # eligible memory records put in the prompt
    news_digest_max: int = 12         # news ids shown in the prompt


@dataclass(frozen=True)
class AgentConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)


def query_backend() -> str:
    return (os.environ.get(QueryConfig().backend_env) or "sqlite").strip().lower()
