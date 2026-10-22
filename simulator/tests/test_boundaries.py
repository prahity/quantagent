"""Static checks: the simulator never imports agent code and vice versa."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _imports(package: str) -> set[str]:
    names: set[str] = set()
    for path in (ROOT / package).rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
    return names


def test_sim_does_not_import_agent_or_server():
    names = _imports("sim")
    assert "agent" not in names and "server" not in names


def test_agent_does_not_import_sim_or_server():
    names = _imports("agent")
    assert "sim" not in names and "server" not in names


def test_contracts_are_standalone():
    names = _imports("contracts")
    assert names.isdisjoint({"sim", "agent", "server"})


def test_no_model_or_browsing_clients_in_sim_runtime():
    runtime = (ROOT / "sim").rglob("*.py")
    forbidden = {"openai", "anthropic", "requests", "selenium", "playwright"}
    for path in runtime:
        if "data" in path.parts:  # collectors run offline with the ingestion identity
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = {a.name.split(".")[0] for a in node.names} if isinstance(node, ast.Import) else {(node.module or "").split(".")[0]}
                assert mods.isdisjoint(forbidden), f"{path} imports {mods & forbidden}"
