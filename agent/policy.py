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
import time
from pathlib import Path

from pydantic import ValidationError

from agent import contract
from agent.config import ModelConfig, PolicyConfig, table_descriptions
from agent.memory import Memory
from agent.query_tool import QueryTool
from agent.schema import AgentStep, DecisionStep, QueryStep

_RULES = """You are a trading agent in an independent market simulator.

Hard rules:
- You may ONLY read released data through the zetaris_query tool. You have no web access.
- You cannot change the data cutoff (as_of); the tool is already bound to it.
- Reply with EXACTLY ONE JSON object (an AgentStep) and nothing else. Two shapes, chosen by
  the "type" discriminator (use the literal key "type", not "action"/"decision" as a key):
    {"type":"query","goal":"<why>","sql":"<single SELECT/WITH over prices, news>"}
    {"type":"decision","orders":[{"symbol":"AAPL","side":"BUY","quantity":10}],
     "reason":"<why>","evidence_query_ids":["q_..."]}
- Each order is EXACTLY {"symbol","side","quantity"}: side in {BUY,SELL}, quantity a positive
  integer. Do NOT include order_id/execute_at/order_type/limit_price; the system fills those
  (MARKET orders at the decision boundary).
- SQL: SELECT/WITH only, single statement, only tables `prices` and `news`
  (already filtered to released_at <= cutoff). No DDL, no writes, no PRAGMA/ATTACH,
  no schema-qualified or sqlite_ names.
- "orders":[] means hold. Prefer hold when evidence is weak.
- Only trade symbols listed in the request's data.symbols. Respect cash and the position cap.
Investigate with a few queries, then commit to one decision."""

_BRIEF_PROMPT = """Read the architecture notes below. In at most 150 words, summarize the RULES
you must follow as the trading agent. Cover: (1) the as_of data cutoff and no-lookahead rule
(you only ever see data released at or before the cutoff); (2) the JSON reply contract -- each
turn is one AgentStep, either a query step or a decision step, discriminated by "type"; (3) the
data access you are allowed -- only released `prices`/`news` through the query tool, no web.
Output plain text only (no JSON, no preamble).

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
        self.run_id = ""
        # Per-decision rollups the runner reads after each decide() (None for non-LLM agents).
        self.last_decision_stats: dict | None = None
        self._dec_tokens = 0
        self._dec_calls = 0
        # Structured-output schema. Sent as guided_json only if the model is configured for it
        # (the hosted endpoint rejects it today); always used to describe the shape + validate.
        self._schema = AgentStep.model_json_schema()
        model_cfg = getattr(getattr(model, "config", None), "use_guided_json", False)
        self._guided_json = self._schema if model_cfg else None

    # ------------------------------------------------------------------ model-call logging
    def _model_log_path(self) -> Path:
        # runs/<run_id>/model_calls.jsonl (sibling of the workspace/ dir).
        return self.workspace_dir.parent / "model_calls.jsonl"

    def _log_model_call(self, record: dict) -> None:
        path = self._model_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_id": self.run_id, **record}, default=str) + "\n")

    def _call_model(self, messages: list[dict], *, request_id: str, step, response_json: bool = False, guided_json: dict | None = None):
        """Call the model, log one key-safe line to model_calls.jsonl, return the ModelResult.

        On error a failure line is still logged, then the exception is re-raised so the
        caller's existing fallback (hold / static brief) applies.
        """
        t0 = time.monotonic()
        try:
            result = self.model.chat_full(messages, response_json=response_json, guided_json=guided_json)
        except Exception as exc:
            self._log_model_call({
                "request_id": request_id, "step": step,
                "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
                "status": None, "retries": None, "finish_reason": None, "truncated": None,
                "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                "content_len": 0, "reasoning_len": 0, "content_preview": "",
                "error": type(exc).__name__,
            })
            raise
        self._dec_calls += 1
        if result.total_tokens:
            self._dec_tokens += result.total_tokens
        self._log_model_call({
            "request_id": request_id, "step": step,
            "latency_ms": round(result.latency_ms, 1),
            "status": result.status, "retries": result.retries,
            "finish_reason": result.finish_reason,
            "truncated": result.finish_reason == "length",
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "content_len": result.content_len,
            "reasoning_len": result.reasoning_len,
            "content_preview": result.raw_content[:500],
            "error": None,
        })
        return result

    # ------------------------------------------------------------------ run start
    def start_run(self, run_id: str, request: dict) -> str:
        """Read the architecture once and write a short operating brief to the workspace."""
        self.run_id = run_id
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        log = self._model_log_path()
        if log.exists():
            log.unlink()  # fresh model-call log every run
        brief = ""
        if self.architecture_text:
            try:
                brief = self._call_model(
                    [{"role": "user", "content": _BRIEF_PROMPT + self.architecture_text[:12000]}],
                    request_id="brief", step=request.get("step"),
                ).content.strip()
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
            + "\n\nJSON SCHEMA (AgentStep):\n" + json.dumps(self._schema)
        )
        user = (
            "DECISION REQUEST:\n" + json.dumps(_prompt_request(request), indent=2)
            + "\n\nRELEVANT NEWS (event ids, query the `news` table for detail):\n" + json.dumps(digest)
            + "\n\nELIGIBLE MEMORY:\n" + json.dumps(eligible, default=str)[:4000]
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        request_id = request["request_id"]
        step = request.get("step")
        self._dec_tokens = 0
        self._dec_calls = 0
        t_decide = time.monotonic()
        try:
            steps = self.config.max_model_steps
            for i in range(steps):
                last = i == steps - 1
                if last:
                    # No budget left to use another query result: force a decision now.
                    messages.append({"role": "user", "content": "No more queries. Return only the decision JSON now."})

                try:
                    result = self._call_model(
                        messages, request_id=request_id, step=step,
                        response_json=True, guided_json=self._guided_json,
                    )
                except Exception as exc:
                    return contract.hold(request, f"model error, holding: {type(exc).__name__}")

                content = result.content.strip()
                # Empty content and/or a max_tokens cut-off: ask once for a compact JSON reply.
                if not content or result.finish_reason == "length":
                    messages.append({"role": "user", "content": (
                        "Your previous reply was empty or cut off before completing. Reply with "
                        "ONLY one compact JSON AgentStep object (no reasoning, no prose).")})
                    continue

                # Parse strictly against the schema, from the content field only.
                try:
                    inner = AgentStep.model_validate_json(content).root
                except ValidationError as exc:
                    messages.append({"role": "assistant", "content": content[:1000]})
                    messages.append({"role": "user", "content": (
                        "Your JSON did not match the AgentStep schema:\n" + str(exc)
                        + "\nReturn exactly one valid AgentStep JSON object.")})
                    continue

                if isinstance(inner, QueryStep):
                    if last:
                        continue  # a query on the final step -> no budget to use it -> hold
                    result_q = query_tool(inner.goal, inner.sql, {})
                    feedback = {
                        "query_id": result_q.query_id,
                        "ok": result_q.ok,
                        "error": result_q.error,
                        "columns": result_q.columns,
                        "rows": result_q.rows[:50],
                        "row_count": result_q.row_count,
                        "truncated": result_q.truncated,
                    }
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": "QUERY RESULT:\n" + json.dumps(feedback, default=str)})
                    continue

                if isinstance(inner, DecisionStep):
                    envelope = self._envelope_from_decision(request, inner)
                    errors = contract.validate_envelope(envelope, request, known, max_orders)
                    if not errors:
                        return envelope
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": (
                        "Your decision was invalid:\n" + "\n".join(errors) + "\nFix it or hold with orders:[].")})
                    continue

            return contract.hold(request, "no valid decision within the step budget")
        finally:
            self.last_decision_stats = {
                "decision_wall_ms": round((time.monotonic() - t_decide) * 1000.0, 1),
                "model_calls": self._dec_calls,
                "total_tokens": self._dec_tokens,
            }

    @staticmethod
    def _envelope_from_decision(request: dict, decision: DecisionStep) -> dict:
        """Build the official V1/V2 envelope in code from a validated DecisionStep.

        The model only supplies symbol/side/quantity; the contract fills order_id, execute_at
        (the decision boundary) and MARKET order_type deterministically.
        """
        orders = [
            contract.make_order(
                request,
                order_id=f"{request['request_id']}-{idx + 1}",
                symbol=o.symbol,
                side=o.side,
                quantity=o.quantity,
            )
            for idx, o in enumerate(decision.orders)
        ]
        return contract.build_envelope(request, orders, decision.reason)


def _prompt_request(request: dict) -> dict:
    """Trim the request to what the model needs; keep ids/window/account/limits/clock."""
    keys = ["schema_version", "run_id", "request_id", "mode", "as_of", "window_start",
            "window_end", "initial_cash", "account", "clock", "limits", "step", "steps_total"]
    out = {k: request[k] for k in keys if k in request}
    out["data"] = {"symbols": request["data"]["symbols"]}
    return out
