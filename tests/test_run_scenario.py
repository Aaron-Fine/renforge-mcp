from __future__ import annotations

from renforge.tools import live
from renforge.state import project_state_dir


def test_run_scenario_set_wait_assert_happy_path(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(
        live,
        "set_var",
        lambda path, name, value: calls.append(("set", name, value)) or {"ok": True},
    )
    monkeypatch.setattr(
        live,
        "wait_until",
        lambda path, **kwargs: {
            "ok": True,
            "matched": {"type": "screen", "value": kwargs.get("screen")},
            "state": {"current_label": "show_thought"},
        },
    )
    monkeypatch.setattr(
        live,
        "eval_expr",
        lambda path, expr: {"ok": True, "value": True},
    )

    result = live.run_scenario(
        str(tmp_path),
        name="Skip stops at choices",
        steps=[
            {"set": {"_preferences.skip_unseen": True}},
            {"wait": {"screen": "choice"}},
            {"assert": {"expr": "config.skipping is None", "message": "Skip must stop"}},
        ],
    )

    assert result["ok"] is True
    assert result["scenario"] == "Skip stops at choices"
    assert result["passed"] == 3
    assert result["failed"] == 0
    assert calls == [("set", "_preferences.skip_unseen", True)]


def test_run_scenario_failure_collects_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setattr(
        live,
        "eval_expr",
        lambda path, expr: {"ok": True, "value": "slow"},
    )
    monkeypatch.setattr(
        live,
        "game_state",
        lambda path, **kwargs: {
            "ok": True,
            "current_label": "show_thought",
            "menu": True,
        },
    )
    monkeypatch.setattr(live, "list_choices", lambda path: {"ok": True, "choices": []})
    monkeypatch.setattr(live, "get_errors", lambda path: {"ok": True, "events": []})
    monkeypatch.setattr(live, "poll_events", lambda path: {"ok": True, "events": []})
    monkeypatch.setattr(live, "screenshot_png", lambda path: b"\x89PNG\r\n")

    result = live.run_scenario(
        str(tmp_path),
        steps=[{"assert": {"expr": "config.skipping is None", "equals": None}}],
        capture_on_failure=True,
    )

    assert result["ok"] is False
    assert result["failed_step"] == 0
    assert "diagnostics" in result["steps"][0] or "screenshot" in result


def test_run_scenario_capture_rejects_parent_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "screenshot_png", lambda path: b"png")

    result = live.run_scenario(
        str(tmp_path),
        steps=[{"capture": {"name": "../outside"}}],
        capture_on_failure=False,
    )

    assert result["ok"] is False
    assert "name" in result["steps"][0]["error"]
    assert not (tmp_path / "outside.png").exists()


def test_run_scenario_capture_rejects_symlinked_capture_directory(monkeypatch, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    project_state_dir(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    (project_state_dir(tmp_path)).mkdir()
    (project_state_dir(tmp_path) / "captures").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(live, "screenshot_png", lambda path: b"png")

    result = live.run_scenario(
        str(tmp_path),
        steps=[{"capture": {"name": "frame"}}],
        capture_on_failure=False,
    )

    assert result["ok"] is False
    assert "symlink" in result["steps"][0]["error"]
    assert not (outside / "frame.png").exists()


def test_run_scenario_capture_overwrites_only_named_file_in_capture_directory(
    monkeypatch, tmp_path
):
    capture_dir = project_state_dir(tmp_path) / "captures"
    capture_dir.mkdir(parents=True)
    target = capture_dir / "frame.png"
    target.write_bytes(b"old")
    sibling = capture_dir / "other.png"
    sibling.write_bytes(b"keep")
    monkeypatch.setattr(live, "screenshot_png", lambda path: b"new")

    result = live.run_scenario(
        str(tmp_path),
        steps=[{"capture": {"name": "frame"}}],
        capture_on_failure=False,
    )

    assert result["ok"] is True
    assert result["steps"][0]["path"] == str(target)
    assert target.read_bytes() == b"new"
    assert sibling.read_bytes() == b"keep"


def test_run_scenario_capture_rejects_symlinked_destination(monkeypatch, tmp_path):
    capture_dir = project_state_dir(tmp_path) / "captures"
    capture_dir.mkdir(parents=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    (capture_dir / "frame.png").symlink_to(outside)
    monkeypatch.setattr(live, "screenshot_png", lambda path: b"new")

    result = live.run_scenario(
        str(tmp_path),
        steps=[{"capture": {"name": "frame"}}],
        capture_on_failure=False,
    )

    assert result["ok"] is False
    assert "symlink" in result["steps"][0]["error"]
    assert outside.read_bytes() == b"keep"


def test_run_scenario_advance_rejects_counts_outside_its_schema(monkeypatch, tmp_path):
    advanced = []
    monkeypatch.setattr(live, "advance", lambda path: advanced.append(1) or {"ok": True})
    monkeypatch.setattr(live, "game_state", lambda path, **kwargs: {"ok": True})
    for bad in ("3", 0, -4, 2.9, True, {"count": 0}, {"count": "2"}):
        result = live.run_scenario(str(tmp_path), steps=[{"advance": bad}])
        assert result["failed"] == 1, bad
    assert advanced == []

    for good, expected in ((None, 1), (3, 3), ({}, 1), ({"count": 2}, 2)):
        advanced.clear()
        result = live.run_scenario(str(tmp_path), steps=[{"advance": good}])
        assert result["passed"] == 1, good
        assert len(advanced) == expected


def test_run_scenario_set_fails_when_the_game_refuses_the_write(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "set_var", lambda path, name, value: {"ok": False, "error": "eval_disabled"})
    monkeypatch.setattr(live, "game_state", lambda path, **kwargs: {"ok": True})
    result = live.run_scenario(str(tmp_path), steps=[{"set": {"gold": 5}}])
    assert result["failed"] == 1


def test_set_var_refuses_a_name_the_store_cannot_hold(monkeypatch, tmp_path):
    # The bridge setattr()s the name on the store: "" or "a.b" would be
    # stored under that literal name and change nothing the game reads.
    monkeypatch.setattr(live, "_with_client", lambda path, handler: {"ok": True})
    for bad in ("", "a.b", "1x", None):
        assert live.set_var(str(tmp_path), bad, 1)["ok"] is False, bad
    assert live.set_var(str(tmp_path), "gold", 1)["ok"] is True
