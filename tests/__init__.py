"""Agent-side test suite (stdlib unittest).

Puts the repository root ahead of the simulator on sys.path so the root `agent` package
is never shadowed by the simulator's own `agent/` harness, while still allowing tests that
exercise the simulator to import `sim` / `contracts`.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIMULATOR = ROOT / "simulator"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SIMULATOR) not in sys.path:
    sys.path.append(str(SIMULATOR))
