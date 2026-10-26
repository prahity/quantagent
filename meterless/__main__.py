"""Check the configured Nightshift runtime.

    python -m meterless
    python -m meterless --detect

Prints online, unauthorized, or offline. The token is never printed.
"""
from __future__ import annotations

import argparse
import os

from meterless.provider import MeterlessRuntime, Probe, auto_detect, load_settings


def _line(probe: Probe) -> str:
    models = ", ".join(probe.models) if probe.models else "none discovered"
    line = f"{probe.status}  {probe.base_url}  models: {models}"
    if probe.detail:
        line += f"\n{probe.detail}"
    return line


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Meterless Nightshift from this app.")
    parser.add_argument("--detect", action="store_true", help="scan the local Nightshift ports")
    parser.add_argument("--say", default="", help="send one plain-text chat on the selected route")
    args = parser.parse_args()
    settings = load_settings()
    token = settings["meterless_runtime_token"]
    if args.detect:
        found = auto_detect(token)
        print(_line(found) if found else "offline  no local runtime on 8088, 8090, 8787, or 11434")
        if found is None or found.status != "online":
            return 1
        settings["base_url"] = found.base_url
    runtime = MeterlessRuntime(settings["base_url"], token, settings["model"])
    probe = runtime.probe()
    if not args.detect:
        print(_line(probe))
    if probe.status != "online":
        runtime.close()
        return 1
    if args.say:
        text = runtime.complete(args.say)
        print(text)
    runtime.close()
    return 0


if __name__ == "__main__":
    os._exit(main())
