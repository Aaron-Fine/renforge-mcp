"""The test suite collects under every pytest import mode.

Test modules share fakes through ``bridge_harness`` and fixtures through
conftest.py, never by importing another test module: that only works while
pytest's default ``prepend`` mode puts tests/ on ``sys.path``.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


def test_no_test_module_imports_another_test_module() -> None:
    offenders = {
        path.name: sorted(name for name in _imported_modules(path) if name.startswith("test_") or name == "conftest")
        for path in sorted(TESTS.glob("*.py"))
    }
    assert {name: found for name, found in offenders.items() if found} == {}


def test_suite_collects_with_importlib_import_mode() -> None:
    root = TESTS.parent
    # Run from the project root: `python -m` puts the working directory on
    # sys.path, which would hide a missing tests/ entry if it were tests/.
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "--import-mode=importlib", "tests"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
