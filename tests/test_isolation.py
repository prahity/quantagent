"""Import-boundary checks: the agent side and the simulator never import each other."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIMULATOR = ROOT / "simulator"


def _imports(package_dir: Path) -> set[str]:
    names: set[str] = set()
    for path in package_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
    return names


class IsolationTest(unittest.TestCase):
    def test_agent_does_not_import_simulator(self):
        for pkg in ("agent", "baselines"):
            names = _imports(ROOT / pkg)
            self.assertNotIn("sim", names, f"{pkg} imports sim")
            self.assertNotIn("simulator", names, f"{pkg} imports simulator")

    def test_simulator_sim_does_not_import_agent(self):
        names = _imports(SIMULATOR / "sim")
        self.assertNotIn("agent", names)
        self.assertNotIn("baselines", names)

    def test_agent_has_no_browsing_or_extra_network_clients(self):
        # The ONLY network client allowed on the agent side is `requests`, in agent/model.py
        # (the model endpoint). No browser/search libraries anywhere.
        forbidden = {"selenium", "playwright", "bs4", "trafilatura", "googlesearch", "httpx"}
        for pkg in ("agent", "baselines"):
            names = _imports(ROOT / pkg)
            self.assertTrue(names.isdisjoint(forbidden), f"{pkg} imports {names & forbidden}")

    def test_only_model_module_reaches_the_network(self):
        net = {"requests", "urllib", "socket", "http"}
        for path in (ROOT / "agent").rglob("*.py"):
            if path.name == "model.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            mods: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods.add(node.module.split(".")[0])
            self.assertTrue(mods.isdisjoint(net), f"{path.name} reaches the network: {mods & net}")


if __name__ == "__main__":
    unittest.main()
