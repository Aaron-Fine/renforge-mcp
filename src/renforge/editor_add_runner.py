"""Live evidence runner for literal add / decorative frame scene-walk adapter."""

from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path
from typing import Any

from renforge.editor.source import (
    analyze_add_position_statement,
    analyze_frame_position_statement,
    apply_add_position_patch,
    apply_frame_position_patch,
)
from renforge.editor_live_common import sha256_file as _sha256_file
from renforge.editor_runner_status import is_reload_committed
from renforge.editor_task0_runner import _require_ok, _source_generation, _wait_for_status
from renforge.tools import live

FIXTURE_SCREEN = "renforge_editor_add_fixture"
TARGET_ID = "add_target"
FRAME_ID = "deco_frame"
FIXTURE_RESOURCE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "live_fixtures"
    / "renforge_editor_add_fixture.rpy"
)

TARGET = {"id": TARGET_ID, "x": 200, "y": 180, "w": 160, "h": 100}
FRAME = {"id": FRAME_ID, "x": 40, "y": 40, "w": 120, "h": 80}
EXPR = {"id": "add_expr", "x": 80, "y": 400, "w": 80, "h": 80}
OVERLAP = {"x": 790, "y": 260}
TRANSFORM = {"x": 250, "y": 460}
SIDEIMAGE = {"id": "add_sideimage", "x": 20, "y": 560, "w": 80, "h": 80}
CHROME = {"x": 40, "y": 16}


def inject_editor_add_resources(project_root: Path) -> Path:
    target = project_root / "game" / "zz_renforge_editor_add_fixture.rpy"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE_RESOURCE, target)
    return target


def _center(item: dict[str, int]) -> tuple[int, int]:
    return (int(item["x"]) + int(item["w"]) // 2, int(item["y"]) + int(item["h"]) // 2)


def _show_fixture(client: Any) -> None:
    last: Any = None
    for _ in range(60):
        last = client.request("editor_task0_start", {"screen": FIXTURE_SCREEN})
        if isinstance(last, dict) and last.get("ok") is True:
            return
        time.sleep(0.1)
    raise AssertionError(f"add fixture did not start: {last!r}")


def _target_line_with_offset(source_text: str, widget_id: str, kind: str) -> tuple[str, int]:
    offset = 0
    prefix = f"{kind} "
    for line in source_text.splitlines(keepends=True):
        if line.lstrip().startswith(prefix) and f'id "{widget_id}"' in line:
            return line, offset
        offset += len(line)
    raise AssertionError(f"source missing {kind} line for {widget_id!r}")


def _parse_xy(source_text: str, widget_id: str, kind: str) -> dict[str, int]:
    line, _ = _target_line_with_offset(source_text, widget_id, kind)
    match = re.search(r"\bxpos\s+(-?\d+)\s+ypos\s+(-?\d+)", line)
    if match is None:
        raise AssertionError(f"target line missing literal xpos/ypos: {line!r}")
    return {"x": int(match.group(1)), "y": int(match.group(2))}


def _wait_analysis(client: Any, widget_id: str, *, unlocked: bool) -> dict[str, Any]:
    if unlocked:
        return _wait_for_status(
            client,
            lambda status: bool(status.get("current_analysis_id"))
            and status.get("selected_widget_id") == widget_id
            and status.get("selected_lock_reason") in (None, ""),
            timeout=10.0,
            poll_name=f"{widget_id} analysis",
        )
    return _wait_for_status(
        client,
        lambda status: status.get("selected_widget_id") == widget_id
        and status.get("selected_lock_reason") not in (None, "", "ANALYZING"),
        timeout=10.0,
        poll_name=f"{widget_id} lock",
    )


def _select_point(client: Any, x: int, y: int) -> dict[str, Any]:
    return client.request("editor_task0_select", {"x": int(x), "y": int(y)})


def _activate_overlay(client: Any) -> None:
    for _ in range(40):
        launcher = client.inspect_screen("_renforge_editor_launcher")
        if launcher.get("active") is True:
            break
        time.sleep(0.25)
    else:
        raise AssertionError("editor launcher never became active")
    click = client.click_element(text="RF", exact=True, screen="_renforge_editor_launcher")
    if click.get("ok") is not True:
        raise AssertionError(f"RF launcher click failed: {click!r}")
    for _ in range(40):
        overlay = client.inspect_screen("_renforge_editor_overlay")
        if overlay.get("active") is True:
            return
        time.sleep(0.05)
    raise AssertionError("editor overlay never became active")


def run_editor_add_live_scenario(
    client: Any,
    *,
    fixture_path: Path,
    project_path: Path,
) -> dict[str, Any]:
    """Live proof for literal add/frame: unlock, locks, public save, chrome."""
    report: dict[str, Any] = {}
    baseline_bytes = fixture_path.read_bytes()
    baseline_sha = _sha256_file(fixture_path)
    baseline_text = baseline_bytes.decode("utf-8")
    target_line, _ = _target_line_with_offset(baseline_text, TARGET_ID, "add")
    parsed = analyze_add_position_statement(target_line, expected_widget_id=TARGET_ID)
    report["fixture_before"] = {
        "sha256": baseline_sha,
        "position": {"x": parsed.xpos, "y": parsed.ypos},
    }

    _show_fixture(client)
    hit_started = time.monotonic()
    _select_point(client, *_center(TARGET))
    report["perf"] = {"hit_test_ms": int((time.monotonic() - hit_started) * 1000)}

    target_select = _require_ok(_select_point(client, *_center(TARGET)), "add_target select")
    observation = target_select.get("observation") or {}
    if observation.get("measurement_method") != "scene_tree_displayable":
        raise AssertionError(f"add select not scene_tree_displayable: {observation!r}")
    analysis = _wait_analysis(client, TARGET_ID, unlocked=True)
    source_key = analysis.get("current_source_key") or {}
    if source_key.get("statement_kind") != "add":
        raise AssertionError(f"expected add statement_kind: {source_key!r}")
    if (analysis.get("current_capabilities") or {}).get("move") is not True:
        raise AssertionError(f"host did not unlock move for add: {analysis!r}")
    report["resolve"] = {
        "statement_kind": source_key.get("statement_kind"),
        "lock_reason": analysis.get("selected_lock_reason"),
        "move": True,
        "measurement_method": observation.get("measurement_method"),
    }

    original = analysis.get("selected_original_position") or [parsed.xpos, parsed.ypos]
    requested_before = [int(original[0]), int(original[1])]
    _require_ok(client.request("editor_task0_key", {"key": "right", "repeat": 24}), "nudge right")
    _require_ok(client.request("editor_task0_key", {"key": "down", "repeat": 16}), "nudge down")
    preview_status = _wait_for_status(
        client,
        lambda status: isinstance(status.get("preview_position"), (list, tuple))
        and list(status.get("preview_position") or []) != requested_before,
        timeout=8.0,
        poll_name="add preview moved",
    )
    requested_after = [int(preview_status["preview_position"][0]), int(preview_status["preview_position"][1])]
    report["preview"] = {
        "requested_before": requested_before,
        "requested_after": requested_after,
    }

    generation_before = _source_generation(analysis)
    if generation_before <= 0:
        generation_before = _source_generation(client.request("editor_task0_status", {}) or {})
    patched_after_public = False
    try:
        save_started = time.monotonic()
        _require_ok(client.click_element(id="rf_save", screen="_renforge_editor_overlay"), "add save")
        save_status = _wait_for_status(
            client,
            lambda status: is_reload_committed(status, generation=generation_before + 1),
            timeout=60.0,
            poll_name="add save complete",
        )
        report["perf"]["save_ms"] = int((time.monotonic() - save_started) * 1000)
        post_save_text = fixture_path.read_text(encoding="utf-8")
        source_after = _parse_xy(post_save_text, TARGET_ID, "add")
        expected_line, offset = _target_line_with_offset(baseline_text, TARGET_ID, "add")
        expected_text = (
            f"{baseline_text[:offset]}"
            f"{apply_add_position_patch(expected_line.encode('utf-8'), parsed, x=requested_after[0], y=requested_after[1]).decode('utf-8')}"
            f"{baseline_text[offset + len(expected_line):]}"
        )
        if post_save_text != expected_text:
            raise AssertionError("patched add fixture disagrees with independent expected content")
        report["patch"] = {
            "before_sha256": baseline_sha,
            "after_sha256": hashlib.sha256(post_save_text.encode("utf-8")).hexdigest(),
            "source_position_after": source_after,
            "matches_independent_expected": True,
        }
        report["reload"] = {
            "ok": True,
            "status_code": save_status.get("status_code"),
            "generation_delta": 1,
        }

        frame_line, _ = _target_line_with_offset(baseline_text, FRAME_ID, "frame")
        frame_parsed = analyze_frame_position_statement(frame_line, expected_widget_id=FRAME_ID)
        frame_select = _select_point(client, *_center(FRAME))
        frame_obs = frame_select.get("observation") or {}
        frame_status = _wait_analysis(client, FRAME_ID, unlocked=True)
        frame_key = frame_status.get("current_source_key") or {}
        report["frame"] = {
            "select_ok": frame_select.get("ok") is True,
            "statement_kind": frame_key.get("statement_kind"),
            "measurement_method": frame_obs.get("measurement_method"),
            "move": (frame_status.get("current_capabilities") or {}).get("move") is True,
            "authored": {"x": frame_parsed.xpos, "y": frame_parsed.ypos},
        }
        if report["frame"]["statement_kind"] != "frame" or report["frame"]["move"] is not True:
            raise AssertionError(f"decorative frame did not unlock: {report['frame']!r} {frame_status!r}")

        locks: dict[str, Any] = {}
        expr_select = _select_point(client, *_center(EXPR))
        expr_lock = expr_select.get("lock_reason")
        if expr_lock != "XPOS_LITERAL_REQUIRED":
            expr_status = _wait_analysis(client, "add_expr", unlocked=False)
            expr_lock = expr_status.get("selected_lock_reason") or expr_select.get("lock_reason")
        locks["expression"] = expr_lock
        if locks["expression"] != "XPOS_LITERAL_REQUIRED":
            raise AssertionError(f"expression xpos lock was not XPOS_LITERAL_REQUIRED: {locks['expression']!r}")

        side_select = _select_point(client, *_center(SIDEIMAGE))
        side_lock = side_select.get("lock_reason") or side_select.get("error")
        if side_lock != "STATEMENT_KIND_MISMATCH" and side_select.get("ok") is True:
            side_status = _wait_analysis(client, "add_sideimage", unlocked=False)
            side_lock = side_status.get("selected_lock_reason") or side_lock
        locks["sideimage"] = side_lock or "UNSELECTED"
        if locks["sideimage"] not in ("STATEMENT_KIND_MISMATCH", "NO_FOCUSABLE_TARGET", "UNSELECTED", "UNMEASURED"):
            raise AssertionError(f"SideImage was not locked: {side_select!r} {locks['sideimage']!r}")

        overlap_select = _select_point(client, OVERLAP["x"], OVERLAP["y"])
        locks["ambiguous"] = overlap_select.get("lock_reason") or overlap_select.get("error")
        if locks["ambiguous"] != "AMBIGUOUS_HIT":
            status = client.request("editor_task0_status", {})
            locks["ambiguous"] = (status or {}).get("selected_lock_reason") or locks["ambiguous"]
        if locks["ambiguous"] != "AMBIGUOUS_HIT":
            raise AssertionError(f"overlap was not AMBIGUOUS_HIT: {overlap_select!r} {locks['ambiguous']!r}")
        if bool(client.eval_expr("_renforge_editor_save_enabled()")):
            raise AssertionError("Save stayed enabled after AMBIGUOUS_HIT")

        transform_select = _select_point(client, TRANSFORM["x"], TRANSFORM["y"])
        transform_status = client.request("editor_task0_status", {})
        transform_caps = (transform_status or {}).get("current_capabilities") or {}
        locks["transform"] = {
            "ok": transform_select.get("ok"),
            "lock_reason": transform_select.get("lock_reason") or (transform_status or {}).get("selected_lock_reason"),
            "move": transform_caps.get("move") is True,
            "widget_id": (transform_status or {}).get("selected_widget_id"),
        }
        if locks["transform"]["move"] is True and locks["transform"]["widget_id"] == "add_transform":
            raise AssertionError(f"Transform add unlocked on this path: {locks['transform']!r}")

        preview_sha_before = _sha256_file(fixture_path)
        moved_target = {
            "x": int(requested_after[0]),
            "y": int(requested_after[1]),
            "w": int(TARGET["w"]),
            "h": int(TARGET["h"]),
        }
        _require_ok(_select_point(client, *_center(moved_target)), "exit-preview select")
        _wait_analysis(client, TARGET_ID, unlocked=True)
        _require_ok(client.request("editor_task0_key", {"key": "right", "repeat": 8}), "exit nudge")
        exit_click = client.click_element(id="rf_exit", screen="_renforge_editor_overlay")
        if exit_click.get("ok") is not True:
            raise AssertionError(f"Exit click failed: {exit_click!r}")
        if _sha256_file(fixture_path) != preview_sha_before:
            raise AssertionError("preview Exit changed fixture bytes")
        report["preview_exit"] = {"sha_unchanged": True}

        public_x = int(requested_after[0]) + int(TARGET["w"]) // 2
        public_y = int(requested_after[1]) + int(TARGET["h"]) // 2
        public_select = live.editor(str(project_path), "select", x=public_x, y=public_y)
        if public_select.get("ok") is not True:
            raise AssertionError(f"public select failed: {public_select!r}")
        if "editor_task0" in str(public_select):
            raise AssertionError("public select leaked editor_task0")
        public_save = live.editor(
            str(project_path),
            "save",
            x=int(requested_after[0]) + 20,
            y=int(requested_after[1]) + 16,
        )
        if public_save.get("ok") is not True:
            raise AssertionError(f"public save failed: {public_save!r}")
        if "editor_task0" in str(public_save):
            raise AssertionError("public save leaked editor_task0")
        public_after = _parse_xy(fixture_path.read_text(encoding="utf-8"), TARGET_ID, "add")
        if public_after == {"x": TARGET["x"], "y": TARGET["y"]}:
            raise AssertionError(f"public save did not patch add xpos/ypos: {public_after!r}")
        report["public_editor"] = {
            "select_ok": True,
            "save_ok": True,
            "source_position_after": public_after,
        }
        chrome_select = _select_point(client, CHROME["x"], CHROME["y"])
        chrome_status = client.request("editor_task0_status", {})
        chrome_move = ((chrome_status or {}).get("current_capabilities") or {}).get("move") is True
        chrome_id = (chrome_status or {}).get("selected_widget_id")
        report["chrome"] = {
            "ok": chrome_select.get("ok"),
            "lock_reason": chrome_select.get("lock_reason") or (chrome_status or {}).get("selected_lock_reason"),
            "move": chrome_move,
            "widget_id": chrome_id,
        }
        if chrome_move is True and chrome_id not in (None, "", "add_focus"):
            raise AssertionError(f"overlay chrome add became editable: {report['chrome']!r}")

        say = client.eval_expr('renpy.show_screen("say", "Eileen", "Hello there.", _layer="screens")')
        time.sleep(0.2)
        say_select = _select_point(client, 40, 680)
        say_status = client.request("editor_task0_status", {})
        say_move = ((say_status or {}).get("current_capabilities") or {}).get("move") is True
        report["say_sideimage"] = {
            "show": say,
            "ok": say_select.get("ok"),
            "lock_reason": say_select.get("lock_reason") or (say_status or {}).get("selected_lock_reason"),
            "move": say_move,
            "widget_id": (say_status or {}).get("selected_widget_id"),
        }
        if say_move is True and report["say_sideimage"]["widget_id"] not in ("who", "what", None, ""):
            raise AssertionError(f"say SideImage unlocked: {report['say_sideimage']!r}")

        report["locks"] = locks
        patched_after_public = fixture_path.read_bytes() != baseline_bytes
    finally:
        fixture_path.write_bytes(baseline_bytes)
    report["byte_identical_undo"] = {
        "matches_baseline": fixture_path.read_bytes() == baseline_bytes,
        "patched_differed": patched_after_public and report.get("patch", {}).get("after_sha256") != baseline_sha,
    }
    return report
