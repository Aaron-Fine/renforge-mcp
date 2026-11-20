"""Offline checks for the say.what and say.who position runners.

The live gates need Ren'Py. These tests drive each runner with a fake bridge
and a fake transaction wait, to pin how a runner reads the wait's
``completed`` flag: an unfinished wait is never a result, even when its last
status says ``reload_committed`` and the bytes already landed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import renforge.editor_say_what_position_runner as what_runner
import renforge.editor_say_who_position_runner as who_runner
from renforge.editor_live_common import TransactionWait

_ORIGIN = (100, 200)
_MOVED = (120, 230)  # each runner drags by (+20, +30)
_COMMITTED_STATUS = {"status_code": "reload_committed", "save_in_progress": False}

_WHAT_GUI = b"gui.dialogue_xpos = gui.scale(268)\ngui.dialogue_ypos = gui.scale(50)\n"
_WHO_GUI = b"define gui.name_xpos = gui.scale(240)\ndefine gui.name_ypos = gui.scale(0)\n"

_RUNNERS = {
    "what": (what_runner, what_runner.run_editor_say_what_style_position_live_scenario, _WHAT_GUI),
    "who": (who_runner, who_runner.run_editor_say_who_style_position_live_scenario, _WHO_GUI),
}

_STEPS = ("commit", "undo", "redo")


class _FakeClient:
    """Enough of the bridge for one runner pass; the dialogue sits at ``position``."""

    def __init__(self) -> None:
        self.position = _ORIGIN
        self.evals: list[str] = []

    def inspect_screen(self, name):
        return {"active": True, "scope": {"who": "Test", "what": "line"}}

    def scene_tree(self, **_kwargs):
        text = "Test dialogue for RenForge #81 Second line Undo line Redo line NameboxSpeaker"
        x, y = self.position
        return {"nodes": [{"text": text, "bounds": {"x": x, "y": y, "width": 50, "height": 20}}]}

    def eval_expr(self, expr):
        self.evals.append(expr)
        if expr.startswith("_renforge_editor_apply_preview"):
            self.position = _MOVED
        return {"ok": True}

    def request(self, name, payload):
        assert name == "editor_task0_status"
        return {
            "selected_lock_reason": None,
            "capabilities": {"move": True},
            "position_mode": "style_gui_namebox",
        }

    def click_element(self, **_kwargs):
        return {"ok": True}


def _run(monkeypatch, tmp_path: Path, which: str, *, completed: dict[str, bool], source_error=None):
    """Run one runner; each wait lands its bytes, then reports ``completed[step]``."""
    module, run, gui_bytes = _RUNNERS[which]
    game = tmp_path / "project" / "game"
    game.mkdir(parents=True)
    gui_path = game / "gui.rpy"
    gui_path.write_bytes(gui_bytes)
    fixture_path = game / "screens.rpy"
    fixture_path.write_bytes(b"screen say(who, what):\n    pass\n")

    client = _FakeClient()
    steps: list[str] = []

    def fake_wait(_client, source_path, expected_bytes, **_kwargs):
        step = _STEPS[len(steps)]
        steps.append(step)
        source_path.write_bytes(expected_bytes)
        client.position = _ORIGIN if step == "undo" else _MOVED
        done = completed.get(step, True)
        return TransactionWait(done, _COMMITTED_STATUS, None, None if done else source_error)

    monkeypatch.setattr(module, "wait_for_transaction", fake_wait)
    monkeypatch.setattr(module, "clear_transactions", lambda _root: None)
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=lambda _seconds: None))
    report = run(client, fixture_path=fixture_path, gui_path=gui_path)
    return report, client, steps


@pytest.mark.parametrize("which", sorted(_RUNNERS))
def test_a_runner_passes_when_every_wait_completes(monkeypatch, tmp_path, which) -> None:
    report, _client, steps = _run(monkeypatch, tmp_path, which, completed={})

    assert steps == list(_STEPS)
    assert report["verdict"] == "pass", report
    assert report["undo_completed"] is True
    assert report["redo_completed"] is True


@pytest.mark.parametrize("which", sorted(_RUNNERS))
def test_an_unfinished_commit_wait_is_a_commit_timeout(monkeypatch, tmp_path, which) -> None:
    report, client, steps = _run(
        monkeypatch, tmp_path, which, completed={"commit": False}, source_error="OSError: busy"
    )

    assert report["verdict"] == "fail"
    assert report["error"] == "commit timeout"
    assert report["last_status"] == _COMMITTED_STATUS
    assert report["source_error"] == "OSError: busy"
    # The post-commit scene steps never ran.
    assert steps == ["commit"]
    assert not any("Second line" in expr for expr in client.evals)


@pytest.mark.parametrize("which", sorted(_RUNNERS))
@pytest.mark.parametrize("step", ["undo", "redo"])
def test_an_unfinished_undo_or_redo_wait_fails_the_verdict(monkeypatch, tmp_path, which, step) -> None:
    report, _client, steps = _run(
        monkeypatch, tmp_path, which, completed={step: False}, source_error="OSError: busy"
    )

    assert steps == list(_STEPS)
    assert report[f"{step}_byte_identical"] is True  # the bytes alone would have passed
    assert report["verdict"] == "fail"
    assert report[f"{step}_completed"] is False
    assert report[f"{step}_source_error"] == "OSError: busy"
