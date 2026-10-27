#!/usr/bin/env python3
"""Serve dashboard/ on localhost and launch trials from the UI.

  python scripts/serve_dashboard.py
  open http://127.0.0.1:8080/
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
TRIALS = ROOT / "context" / "trials"
LOG = TRIALS / "dashboard-launches.log"
_lock = threading.Lock()
_active: subprocess.Popen | None = None


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _launch(start: str, end: str) -> dict:
    global _active
    start_d = _parse_day(start)
    end_d = _parse_day(end)
    if end_d < start_d:
        return {"ok": False, "error": "End date must be on or after start."}
    days = (end_d - start_d).days + 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = TRIALS / f"ui-{start_d.isoformat()}-{end_d.isoformat()}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "trial.log"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    # Load .env if present without printing secrets.
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            env.setdefault(key.strip(), val.strip().strip('"').strip("'"))

    cmd = [
        env.get("PYTHON", "python3"), "-u", str(ROOT / "simulator" / "trial.py"),
        "--out", str(out),
        "--days", str(days),
        "--start", start_d.isoformat(),
        "--thinking", "off",
        "--model", "nvidia/nemotron-3.5-lightning-30b-a3b",
        "--arms", "memory,nomem",
    ]
    with _lock:
        if _active is not None and _active.poll() is None:
            return {"ok": False, "error": "A trial is already running. Wait for it to finish."}
        log_fh = log_path.open("w")
        _active = subprocess.Popen(
            cmd,
            cwd=str(ROOT / "simulator"),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps({
                "pid": _active.pid,
                "start": start_d.isoformat(),
                "end": end_d.isoformat(),
                "out": str(out.relative_to(ROOT)),
                "launched_at": datetime.now().isoformat(timespec="seconds"),
            }) + "\n")
    return {
        "ok": True,
        "message": f"Trial launched for {start_d} → {end_d} ({days} days). Output: {out.relative_to(ROOT)}",
        "out": str(out.relative_to(ROOT)),
        "pid": _active.pid,
        "log": str(log_path.relative_to(ROOT)),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/trial":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "Invalid JSON body."})
            return
        try:
            result = _launch(str(body.get("start") or ""), str(body.get("end") or ""))
        except ValueError:
            self._json(400, {"ok": False, "error": "Dates must be YYYY-MM-DD."})
            return
        self._json(200 if result.get("ok") else 409, result)

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    if not (DASHBOARD / "index.html").exists():
        raise SystemExit("dashboard/index.html missing. Run scripts/build_dashboard.py first.")
    host, port = "127.0.0.1", 8080
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Quantagent dashboard at http://{host}:{port}/")
    print("POST /api/trial with {start, end} launches a local trial.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
