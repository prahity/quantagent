"""Append-only, run-scoped memory with an availability cutoff."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.memory import Memory


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "memory.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_retrieval_respects_cutoff(self):
        mem = Memory(self.path, "run_a")
        mem.append({"kind": "decision", "note": "early", "available_at": "2025-01-28T14:30:00Z"})
        mem.append({"kind": "decision", "note": "late", "available_at": "2025-01-28T16:30:00Z"})
        early = mem.retrieve("2025-01-28T15:00:00Z")
        self.assertEqual([r["note"] for r in early], ["early"])
        both = mem.retrieve("2025-01-28T17:00:00Z")
        self.assertEqual({r["note"] for r in both}, {"early", "late"})

    def test_requires_available_at(self):
        mem = Memory(self.path, "run_a")
        with self.assertRaises(ValueError):
            mem.append({"kind": "decision", "note": "no cutoff"})

    def test_is_append_only_and_persisted(self):
        mem = Memory(self.path, "run_a")
        mem.append({"available_at": "2025-01-28T14:30:00Z", "note": "x"})
        mem.append({"available_at": "2025-01-28T15:30:00Z", "note": "y"})
        lines = [x for x in self.path.read_text().splitlines() if x.strip()]
        self.assertEqual(len(lines), 2)

    def test_fresh_memory_each_run(self):
        Memory(self.path, "run_a").append({"available_at": "2025-01-28T14:30:00Z", "note": "old"})
        fresh = Memory(self.path, "run_b")  # same path, new run -> wiped
        self.assertEqual(len(fresh), 0)
        self.assertEqual(fresh.retrieve("2025-02-01T00:00:00Z"), [])


if __name__ == "__main__":
    unittest.main()
