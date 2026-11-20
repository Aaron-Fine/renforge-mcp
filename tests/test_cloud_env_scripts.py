from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LAUNCH = _ROOT / "scripts" / "smoke_renpy_launch.py"


def test_smoke_launch_script_resolves_demo_from_repo() -> None:
    """The committed launcher must root demo_game at the repository tree."""
    spec = importlib.util.spec_from_file_location("smoke_renpy_launch", _LAUNCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    demo = module.demo_project_path()
    # The demo path must live inside this repository checkout regardless of
    # where that checkout is rooted (CI workspace, home dir, or a /tmp
    # worktree). Assert the repo-relative invariant rather than a fixed prefix.
    assert demo == _ROOT / "examples" / "demo_game"
    assert demo.is_relative_to(_ROOT)
    assert demo.is_dir()
    assert (demo / "game" / "script.rpy").is_file()


def test_sandbox_prerequisites_name_every_tool_the_contract_tests_run(monkeypatch) -> None:
    """The sandbox contract runs setsid in the guest; a host without it must fail on the prerequisite."""
    import shutil
    import sys

    monkeypatch.syspath_prepend(str(_ROOT / "scripts"))
    sys.modules.pop("sandbox_test_support", None)
    import sandbox_test_support as sandbox

    real_which = shutil.which
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None if name == "setsid" else real_which(name))
    assert "setsid" in sandbox.missing_requirements()
    sys.modules.pop("sandbox_test_support", None)
