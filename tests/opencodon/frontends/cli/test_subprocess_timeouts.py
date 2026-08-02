"""Tests for subprocess.run() timeout coverage in CLI utilities."""
import ast
from pathlib import Path

import pytest


# Parameterise over every CLI module that calls subprocess.run
_CLI_MODULES = [
    "src/opencodon/frontends/cli/doctor.py",
    "src/opencodon/frontends/cli/status.py",
    "src/opencodon/frontends/cli/clipboard.py",
    "src/opencodon/frontends/cli/banner.py",
]

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _subprocess_run_calls(filepath: str) -> list[dict]:
    """Parse a Python file and return info about subprocess.run() calls."""
    source = Path(filepath).read_text()
    tree = ast.parse(source, filename=filepath)
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "run"
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"):
            has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
            calls.append({"line": node.lineno, "has_timeout": has_timeout})
    return calls


@pytest.mark.parametrize("filepath", _CLI_MODULES)
def test_all_subprocess_run_calls_have_timeout(filepath):
    """Every subprocess.run() call in CLI modules must specify a timeout."""
    path = _REPO_ROOT / filepath
    assert path.exists(), (
        f"{filepath} not found — update _CLI_MODULES after moving CLI modules "
        "(a silent skip here disables timeout coverage entirely)"
    )
    calls = _subprocess_run_calls(str(path))
    missing = [c for c in calls if not c["has_timeout"]]
    assert not missing, (
        f"{filepath} has subprocess.run() without timeout at "
        f"line(s): {[c['line'] for c in missing]}"
    )
