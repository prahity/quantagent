"""Strategy notebook kept across runs. After each completed period, Nemotron sends an update through Meterless.

The update may add a rule, amend a rule, or drop a rule. Every rule the update does not name stays word for word. The notebook only ever sees periods that have finished, so a run never reads lessons drawn from its own future.
"""
from __future__ import annotations

import json
import re

from meterless.provider import MeterlessRuntime

_RULE = re.compile(r"^r(\d+): (.*)$")
_EVIDENCE = re.compile(r"^evidence: (.*)$")
_FIELDS = {"add", "amend", "drop"}
_MAX_ADDS = 2
_MAX_TEXT = 400

_PROMPT = """You update the strategy notebook for an AI trading agent. Its goal is to end the next window with more cash. Cash and open positions are both counted at the final close. It trades US large-cap stocks and ETFs on daily bars, long only. The simulator allows up to 25% of equity per position, and market orders fill at the next open.
Below are the current notebook and the record of the period that just finished. The record's numbers were computed by the simulator: returns, profit by symbol, average share of equity invested, days fully in cash, and buy-and-hold of the same symbols.

Return one JSON object and no other text:
{{"add":[{{"text":"<new rule>","evidence":"<period and the record's numbers>"}}],"amend":[{{"id":"r1","text":"<replacement instruction, omit to keep the instruction>","evidence":"<new evidence line to append>"}}],"drop":[{{"id":"r2","because":"<period and the numbers that contradict the rule>"}}]}}

Rules:
- A rule you do not name stays exactly as written. Do not restate it, renumber it, or return the notebook.
- Omit add, amend, or drop when you have nothing for that list. {{}} keeps the notebook unchanged.
- add at most {max_adds} rules. If the notebook is empty, use add. Mark each new rule unproven in its evidence. One period does not prove a rule.
- amend a rule this period's numbers change. New evidence is appended. Earlier evidence stays. Include text only when the instruction itself must change. An amend for an id that is not in the notebook is saved as a new rule.
- drop only when this period's numbers contradict that rule. Quote those numbers in because.
- Quote only numbers that appear in the record or the current notebook. Do not estimate or invent any figure.
- When buy-and-hold beat the agent while average_invested_pct_of_equity was low, the cause was idle cash. Address it. Do not add caution.
- Do not lower the 25% per-position limit or cap total investment unless the record shows losses from oversized positions.

Current notebook:
{notes}

Period that just finished:
{period}
"""


def parse(notes: str) -> tuple[str, list[dict]]:
    """Split a notebook into untouched leading text and its rules."""
    if not notes.strip():
        return "", []
    preamble: list[str] = []
    rules: list[dict] = []
    current: dict | None = None
    in_evidence = False
    started = False
    for line in notes.splitlines():
        rule_match = _RULE.match(line)
        evidence_match = _EVIDENCE.match(line)
        if rule_match:
            started = True
            in_evidence = False
            current = {"id": f"r{rule_match.group(1)}", "text": rule_match.group(2).strip(), "evidence": []}
            rules.append(current)
            continue
        if not started:
            preamble.append(line)
            continue
        if evidence_match:
            if current is None:
                raise ValueError("evidence line before any rule")
            current["evidence"].append(evidence_match.group(1).strip())
            in_evidence = True
            continue
        if not line.strip():
            in_evidence = False
            continue
        if current is None:
            preamble.append(line)
        elif in_evidence and current["evidence"]:
            current["evidence"][-1] += " " + line.strip()
        else:
            current["text"] += " " + line.strip()
    ids = [rule["id"] for rule in rules]
    if len(ids) != len(set(ids)):
        raise ValueError("notebook has duplicate rule ids")
    return "\n".join(preamble).strip(), rules


def render(preamble: str, rules: list[dict]) -> str:
    blocks = []
    if preamble:
        blocks.append(preamble)
    for rule in rules:
        lines = [f"{rule['id']}: {rule['text']}"]
        lines.extend(f"evidence: {line}" for line in rule["evidence"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("notebook update was not a JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("notebook update was not a JSON object") from exc
    if not isinstance(data, dict):
        raise ValueError("notebook update was not a JSON object")
    extra = set(data) - _FIELDS
    if extra:
        raise ValueError(f"notebook update has unknown fields: {sorted(extra)}")
    return data


def _entries(data: dict, key: str) -> list:
    entries = data.get(key) or []
    if not isinstance(entries, list):
        raise ValueError(f"{key} must be a list")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{key} entries must be objects")
    return entries


def _line(entry: dict, key: str, required: bool) -> str:
    if key not in entry:
        if required:
            raise ValueError(f"{key} is required")
        return ""
    value = entry[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = " ".join(value.split())
    if required and not value:
        raise ValueError(f"{key} is required")
    if len(value) > _MAX_TEXT:
        raise ValueError(f"{key} is longer than {_MAX_TEXT} characters")
    return value


def apply(notes: str, raw: str) -> str:
    """Apply one update. Rules the update does not name are copied unchanged."""
    data = _object(raw)
    preamble, rules = parse(notes)
    by_id = {rule["id"]: rule for rule in rules}
    adds = _entries(data, "add")
    amends = _entries(data, "amend")
    drops = _entries(data, "drop")
    kept_amends = []
    for entry in amends:
        if entry.get("id") in by_id:
            kept_amends.append(entry)
            continue
        text = _line(entry, "text", required=False) or _line(entry, "evidence", required=False)
        evidence = _line(entry, "evidence", required=False) or "unproven"
        if text:
            adds.append({"text": text, "evidence": evidence})
    amends = kept_amends
    if len(adds) > _MAX_ADDS:
        raise ValueError(f"an update may add at most {_MAX_ADDS} rules")

    seen = [int(rule["id"][1:]) for rule in rules]
    dropped = []
    for entry in drops:
        rule_id = entry.get("id")
        if rule_id not in by_id:
            raise ValueError(f"cannot drop unknown rule {rule_id}")
        _line(entry, "because", required=True)
        dropped.append(rule_id)
    rules = [rule for rule in rules if rule["id"] not in set(dropped)]
    by_id = {rule["id"]: rule for rule in rules}

    for entry in amends:
        rule_id = entry.get("id")
        if rule_id not in by_id:
            raise ValueError(f"cannot amend unknown rule {rule_id}")
        text = _line(entry, "text", required=False)
        evidence = _line(entry, "evidence", required=False)
        if not text and not evidence:
            raise ValueError(f"amend of {rule_id} changes nothing")
        if text:
            by_id[rule_id]["text"] = text
        if evidence:
            by_id[rule_id]["evidence"].append(evidence)

    next_id = 1 + max(seen, default=0)
    for entry in adds:
        text = _line(entry, "text", required=True)
        evidence = _line(entry, "evidence", required=True)
        rules.append({"id": f"r{next_id}", "text": text, "evidence": [evidence]})
        next_id += 1
    return render(preamble, rules)


def update(runtime: MeterlessRuntime, notes: str, period: dict) -> str:
    raw = runtime.complete(_PROMPT.format(
        max_adds=_MAX_ADDS,
        notes=notes or "(empty, this is the first period)",
        period=json.dumps(period, indent=1),
    ))
    return apply(notes, raw)
