#!/usr/bin/env python3
"""Run memory-arm variants against no-memory for a fixed wall-clock budget.

Writes one line per finished arm/window into master.jsonl and a short status
file so a later process can summarize without re-running.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "context" / "trials" / "memory-bakeoff"
SEED = OUT / "seed-deploy.md"
MASTER = OUT / "master.jsonl"
STATUS = OUT / "status.json"
LOG = OUT / "bakeoff.log"

# About 21 calendar days ≈ 15 trading sessions. Earlier 30d dual-arm runs were ~13 min.
WINDOWS = [
    ("2024-01-16", 21),
    ("2024-02-12", 21),
    ("2024-03-11", 21),
    ("2024-04-08", 21),
]

# Ideas that try to make Meterless memory beat no-memory.
VARIANTS = [
    {
        "name": "baseline-sql",
        "arms": "memory,nomem",
        "recall": "sql",
        "prompt_style": "default",
        "in_run_memory": 5,
        "seed": False,
        "why": "Current memory: model SQL + empty notebook + short order log.",
    },
    {
        "name": "brief",
        "arms": "memory",
        "recall": "brief",
        "prompt_style": "default",
        "in_run_memory": 5,
        "seed": False,
        "why": "Replace flaky model SQL with a fixed 21-session price brief.",
    },
    {
        "name": "brief-seed",
        "arms": "memory",
        "recall": "brief",
        "prompt_style": "default",
        "in_run_memory": 5,
        "seed": True,
        "why": "Brief plus a seeded notebook so rules exist on day one.",
    },
    {
        "name": "deploy-brief-seed",
        "arms": "memory",
        "recall": "brief",
        "prompt_style": "deploy",
        "in_run_memory": 5,
        "seed": True,
        "why": "Brief + seed + prompt that idle cash is the main loss vs buy-and-hold.",
    },
    {
        "name": "equal-brief-seed",
        "arms": "memory",
        "recall": "brief",
        "prompt_style": "equal",
        "in_run_memory": 5,
        "seed": True,
        "why": "Brief + seed + equal-weight entry when flat (closer to the benchmark).",
    },
    {
        "name": "notes-seed-deploy",
        "arms": "memory",
        "recall": "notes",
        "prompt_style": "deploy",
        "in_run_memory": 8,
        "seed": True,
        "why": "Notebook + deploy prompt only; no SQL/brief (cheaper memory path).",
    },
    {
        "name": "longlog-deploy-brief",
        "arms": "memory",
        "recall": "brief",
        "prompt_style": "deploy",
        "in_run_memory": 15,
        "seed": True,
        "why": "Longer in-run order log so the agent sees its own holds and fills.",
    },
]


def run_trial(variant: dict, start: str, days: int, deadline: float) -> int:
    label = f"{start}+{days}d"
    out = OUT / variant["name"]
    out.mkdir(parents=True, exist_ok=True)
    if (out / "results.jsonl").exists():
        done = {(r["arm"], r["month"]) for r in map(json.loads, (out / "results.jsonl").read_text().splitlines())}
        if ("memory", label) in done and (variant["arms"] == "memory" or ("nomem", label) in done):
            print(f"skip {variant['name']} {label}", flush=True)
            return 0
    remaining = int(deadline - time.time())
    if remaining < 120:
        print(f"stop before {variant['name']} {label}: {remaining}s left", flush=True)
        return 2
    cmd = [
        sys.executable, "-u", str(ROOT / "simulator" / "trial.py"),
        "--out", str(out),
        "--days", str(days),
        "--start", start,
        "--thinking", "off",
        "--model", "nvidia/nemotron-3.5-lightning-30b-a3b",
        "--arms", variant["arms"],
        "--recall", variant["recall"],
        "--prompt-style", variant["prompt_style"],
        "--in-run-memory", str(variant["in_run_memory"]),
        "--retries", "3",
    ]
    if variant["seed"]:
        cmd.extend(["--seed-notes", str(SEED)])
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    print(f"\n=== {variant['name']} {label}  why={variant['why']} ===", flush=True)
    with LOG.open("a") as log:
        log.write(f"\n=== {variant['name']} {label} ===\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT / "simulator"),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()
            if time.time() >= deadline and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                print("deadline hit; stopped child", flush=True)
                return 2
        return proc.wait()


def harvest() -> None:
    rows = []
    for variant in VARIANTS:
        path = OUT / variant["name"] / "results.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            row = json.loads(line)
            row["variant"] = variant["name"]
            row["why"] = variant["why"]
            rows.append(row)
    MASTER.write_text("".join(json.dumps(r) + "\n" for r in rows))
    by_variant: dict[str, list] = {}
    for r in rows:
        by_variant.setdefault(r["variant"], []).append(r)
    summary = []
    for name, items in by_variant.items():
        mem = [r for r in items if r["arm"] == "memory"]
        nom = [r for r in items if r["arm"] == "nomem"]
        mean = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
        summary.append({
            "variant": name,
            "memory_windows": len(mem),
            "memory_mean_return": mean([r["return_pct"] for r in mem]),
            "memory_mean_excess": mean([r["excess_pct"] for r in mem]),
            "nomem_windows": len(nom),
            "nomem_mean_return": mean([r["return_pct"] for r in nom]),
            "beats_bench": sum(r["excess_pct"] > 0 for r in mem),
        })
    STATUS.write_text(json.dumps({
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows": len(rows),
        "summary": summary,
        "variants": VARIANTS,
        "windows": WINDOWS,
    }, indent=2))
    print(json.dumps(summary, indent=2), flush=True)


def main() -> int:
    hours = float(os.environ.get("BAKEOFF_HOURS", "2"))
    deadline = time.time() + hours * 3600
    OUT.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps({"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "deadline": deadline}))
    # Prefer finishing one window across all variants, then the next window.
    for start, days in WINDOWS:
        for variant in VARIANTS:
            code = run_trial(variant, start, days, deadline)
            harvest()
            if code == 2:
                harvest()
                return 0
            if code not in (0, None):
                print(f"variant {variant['name']} exited {code}", flush=True)
    # Chain: three months with notebook carry, deploy+brief+seed.
    if time.time() + 900 < deadline:
        chain = {
            "name": "chain-deploy-brief",
            "arms": "memory,nomem",
            "recall": "brief",
            "prompt_style": "deploy",
            "in_run_memory": 8,
            "seed": True,
            "why": "Notebook carries across Feb–Apr 2024 so later months use earlier rules.",
        }
        out = OUT / chain["name"]
        out.mkdir(parents=True, exist_ok=True)
        remaining = int(deadline - time.time())
        cmd = [
            sys.executable, "-u", str(ROOT / "simulator" / "trial.py"),
            "--out", str(out),
            "--first", "2024-02",
            "--last", "2024-04",
            "--thinking", "off",
            "--model", "nvidia/nemotron-3.5-lightning-30b-a3b",
            "--arms", chain["arms"],
            "--recall", chain["recall"],
            "--prompt-style", chain["prompt_style"],
            "--in-run-memory", str(chain["in_run_memory"]),
            "--seed-notes", str(SEED),
            "--retries", "3",
            "--workers", "3",
        ]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        print(f"\n=== chain-deploy-brief Feb–Apr ({remaining}s budget) ===", flush=True)
        with LOG.open("a") as log:
            proc = subprocess.Popen(cmd, cwd=str(ROOT / "simulator"), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(line)
                log.write(line)
                if time.time() >= deadline and proc.poll() is None:
                    proc.terminate()
                    break
            proc.wait(timeout=60)
        # tag chain rows
        path = out / "results.jsonl"
        if path.exists():
            tagged = []
            for line in path.read_text().splitlines():
                row = json.loads(line)
                row["variant"] = chain["name"]
                row["why"] = chain["why"]
                tagged.append(row)
            # append into master via harvest after writing variant field into a sidecar
            (out / "variant.json").write_text(json.dumps(chain))
    harvest()
    # Merge chain into master if present
    chain_path = OUT / "chain-deploy-brief" / "results.jsonl"
    if chain_path.exists():
        why = "Notebook carries across Feb–Apr 2024 so later months use earlier rules."
        extra = []
        for line in chain_path.read_text().splitlines():
            row = json.loads(line)
            row["variant"] = "chain-deploy-brief"
            row["why"] = why
            extra.append(row)
        with MASTER.open("a") as fh:
            for row in extra:
                fh.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
