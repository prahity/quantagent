"""The Nemotron trading policy.

At run start the policy reads the architecture once and asks the model to write a short
operating brief into its workspace. At each decision it is given: the rules, its brief, the
decision-request JSON for time t, the released-feed table descriptions, the relevant-news
digest and eligible memory. It replies with one JSON object per step:

    {"type": "query",    "goal": "...", "sql": "...", "params": {...}}
    {"type": "decision", "orders": [...], "reason": "...", "evidence_query_ids": [...]}

A `query` runs the cutoff-bound query tool and the result is fed back. A `decision` is turned
into the fixed JSON response envelope, validated locally, and returned. Anything that fails
(no model, bad JSON, invalid envelope, budget exhausted) falls back to a hold.

Imports no simulator code. Its only outside channel is the model endpoint, via agent.model.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent import contract
from agent.config import PolicyConfig, table_descriptions
from agent.memory import Memory
from agent.query_tool import QueryTool

_RULES = """You are a trading agent in an independent market simulator.

Hard rules:
- You may ONLY read released data through the zetaris_query tool. You have no web access.
- You cannot change the data cutoff (as_of); the tool is already bound to it.
- Reply with EXACTLY ONE JSON object and nothing else. Two shapes are allowed:
    {"type":"query","goal":"<why>","sql":"<SELECT/WITH over tables prices, news>","params":{}}
    {"type":"decision","orders":[...],"reason":"<why>","evidence_query_ids":["q_..."]}
- SQL: SELECT/WITH only, single statement, only tables `prices` and `news`
  (already filtered to released_at <= cutoff). No DDL, no writes, no PRAGMA/ATTACH,
  no schema-qualified or sqlite_ names.
- Each order object is EXACTLY:
    {"order_id","execute_at","symbol","side","quantity","order_type","limit_price"}
  side in {BUY,SELL}; quantity a positive integer; order_type "MARKET" (limit_price null)
  or "LIMIT" (positive limit_price). In V1 execute_at must equal window_start; in V2 it
  must be inside [window_start, window_end).
- `orders: []` means hold. Prefer hold when evidence is weak.
- Only trade symbols listed in the request's data.symbols. Respect cash and the position cap.
Investigate with a few queries, then commit to one decision."""

_BRIEF_PROMPT = """Read the architecture notes below. In at most 150 words, write an operating
brief for yourself as the trading agent: what to query before deciding, how to weigh evidence,
and when to hold. Output plain text only (no JSON, no preamble).

ARCHITECTURE NOTES:
"""


def extract_json(text: str) -> dict | None:
    """Return the first balanced top-level JSON object in `text`, or None."""
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    start = -1
    return None


class NemotronPolicy:
    name = "nemotron"

    def __init__(self, model, workspace_dir: str | Path, architecture_text: str | None = None, config: PolicyConfig | None = None):
        self.model = model
        self.workspace_dir = Path(workspace_dir)
        self.architecture_text = architecture_text or ""
        self.config = config or PolicyConfig()
        self.brief = ""

    # ------------------------------------------------------------------ run start
    def start_run(self, run_id: str, request: dict) -> str:
        """Read the architecture once and write a short operating brief to the workspace."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        brief = ""
        if self.architecture_text:
            try:
                brief = self.model.chat(
                    [{"role": "user", "content": _BRIEF_PROMPT + self.architecture_text[:12000]}]
                ).strip()
            except Exception as exc:  # model unavailable -> static brief, run still proceeds
                brief = f"(brief unavailable: {type(exc).__name__}) Query recent prices and news, hold when evidence is weak."
        self.brief = brief or "Query recent prices and news before deciding; hold when evidence is weak."
        (self.workspace_dir / "operating_brief.md").write_text(self.brief, encoding="utf-8")
        return self.brief

    # ------------------------------------------------------------------ per decision
    def decide(self, request: dict, query_tool: QueryTool, memory: Memory) -> dict:
        known = set(request["data"]["symbols"])
        max_orders = request["limits"]["max_orders_per_request"]
        eligible = memory.retrieve(request["as_of"], limit=self.config.memory_max_records)
        digest = request["data"]["news_event_ids"][: self.config.news_digest_max]

        system = (
            _RULES
            + "\n\nOPERATING BRIEF:\n" + self.brief
            + "\n\nREADABLE TABLES:\n" + table_descriptions()
        )
        user = (
            "DECISION REQUEST:\n" + json.dumps(_prompt_request(request), indent=2)
            + "\n\nRELEVANT NEWS (event ids, query the `news` table for detail):\n" + json.dumps(digest)
            + "\n\nELIGIBLE MEMORY:\n" + json.dumps(eligible, default=str)[:4000]
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        for _ in range(self.config.max_model_steps):
            try:
                raw = self.model.chat(messages, response_json=True)
            except Exception as exc:
                return contract.hold(request, f"model error, holding: {type(exc).__name__}")
            obj = extract_json(raw)
            if obj is None or "type" not in obj:
                messages.append({"role": "user", "content": "Reply with exactly one JSON object with a 'type' field."})
                continue

            if obj["type"] == "query":
                result = query_tool(obj.get("goal", ""), obj.get("sql", ""), obj.get("params") or {})
                feedback = {
                    "query_id": result.query_id,
                    "ok": result.ok,
                    "error": result.error,
                    "columns": result.columns,
                    "rows": result.rows[:50],
                    "row_count": result.row_count,
                    "truncated": result.truncated,
                }
                messages.append({"role": "assistant", "content": json.dumps(obj)})
                messages.append({"role": "user", "content": "QUERY RESULT:\n" + json.dumps(feedback, default=str)})
                continue

            if obj["type"] == "decision":
                orders = obj.get("orders", []) or []
                envelope = contract.build_envelope(request, orders, obj.get("reason", ""))
                errors = contract.validate_envelope(envelope, request, known, max_orders)
                if not errors:
                    return envelope
                messages.append({"role": "assistant", "content": json.dumps(obj)})
                messages.append({"role": "user", "content": "Your decision was invalid:\n" + "\n".join(errors) + "\nFix it or hold."})
                continue

            messages.append({"role": "user", "content": "Unknown 'type'. Use 'query' or 'decision'."})

        return contract.hold(request, "no valid decision within the step budget")


def _prompt_request(request: dict) -> dict:
    """Trim the request to what the model needs; keep ids/window/account/limits/clock."""
    keys = ["schema_version", "run_id", "request_id", "mode", "as_of", "window_start",
            "window_end", "initial_cash", "account", "clock", "limits", "step", "steps_total"]
    out = {k: request[k] for k in keys if k in request}
    out["data"] = {"symbols": request["data"]["symbols"]}
    return out
