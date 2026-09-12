"""Live test runner for say.who / namebox style position."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from renforge.bridge.client import BridgeClient, BridgeProtocolError


FIXTURE_SCREEN = "say"
TARGET_ID = "who"
SPEAKER = "NameboxSpeaker"


def _text_bounds(client: BridgeClient, text: str) -> dict[str, int] | None:
    tree = client.scene_tree(types=["text"], detail="semantic")
    nodes = tree.get("nodes") if isinstance(tree, dict) else []
    for node in nodes:
        if not isinstance(node, dict) or text not in str(node.get("text") or ""):
            continue
        bounds = node.get("bounds")
        if isinstance(bounds, dict):
            return {
                "x": int(bounds["x"]),
                "y": int(bounds["y"]),
                "width": int(bounds["width"]),
                "height": int(bounds["height"]),
            }
    return None


def _wait_for_transaction(
    client: BridgeClient,
    source_path: Path,
    expected_bytes: bytes,
    *,
    timeout: float = 60.0,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        try:
            status = client.request("editor_task0_status", {})
            last_status = status
            if (
                status.get("status_code") == "reload_committed"
                and status.get("save_in_progress") is False
                and source_path.read_bytes() == expected_bytes
            ):
                return status
        except (BridgeProtocolError, OSError):
            pass
        time.sleep(0.25)
    return last_status


def _within_one_pixel(bounds: dict[str, int] | None, expected: list[int]) -> bool:
    return bool(
        bounds
        and abs(bounds["x"] - expected[0]) <= 1
        and abs(bounds["y"] - expected[1]) <= 1
    )


def run_editor_say_who_style_position_live_scenario(
    client: BridgeClient,
    *,
    fixture_path: Path,
    gui_path: Path,
    screenshot_path: Path | None = None,
) -> dict[str, Any]:
    """Select, unlock, preview, save, reload, undo say.who namebox position."""
    report: dict[str, Any] = {}

    gui_source_initial = gui_path.read_bytes()
    screens_source_initial = fixture_path.read_bytes()

    transaction_root = fixture_path.parent.parent / ".renforge" / "editor-transactions"
    if transaction_root.exists():
        import shutil
        try:
            shutil.rmtree(transaction_root)
        except OSError:
            pass

    say_active = client.inspect_screen("say")
    say_scope_before = {
        key: (say_active.get("scope") or {}).get(key)
        for key in ("who", "what")
    }
    report["say_screen_active"] = say_active.get("active") is True
    if not report["say_screen_active"]:
        report["verdict"] = "fail"
        report["error"] = "say screen not active"
        return report

    who_bounds = _text_bounds(client, SPEAKER)
    report["who_bounds"] = who_bounds
    if not who_bounds:
        report["verdict"] = "fail"
        report["error"] = "say.who not found in scene tree"
        return report

    select_x = who_bounds["x"] + 10
    select_y = who_bounds["y"] + 10
    select_result = client.eval_expr(
        f'_renforge_editor_select({select_x}, {select_y})'
    )
    report["select"] = select_result

    for _ in range(100):
        status = client.request("editor_task0_status", {})
        lock_reason = status.get("selected_lock_reason")
        if lock_reason != "ANALYZING":
            break
        time.sleep(0.1)
    else:
        report["verdict"] = "fail"
        report["error"] = "analysis timeout (still ANALYZING after 10s)"
        return report

    report["unlock"] = {
        "position_mode": status.get("position_mode"),
        "capabilities": status.get("capabilities"),
        "selected_lock_reason": status.get("selected_lock_reason"),
        "save_enabled": status.get("save_enabled"),
    }

    capabilities = status.get("capabilities") or {}
    move_unlocked = capabilities.get("move") is True
    report["move_unlocked"] = move_unlocked
    if not move_unlocked:
        report["verdict"] = "fail"
        report["error"] = "say.who not unlocked for move"
        return report
    if status.get("position_mode") != "style_gui_namebox":
        report["verdict"] = "fail"
        report["error"] = f"expected style_gui_namebox, got {status.get('position_mode')}"
        return report

    new_x = who_bounds["x"] + 20
    new_y = who_bounds["y"] + 30
    expected_screen_position = [new_x, new_y]

    preview_result = client.eval_expr(
        f'_renforge_editor_apply_preview({new_x}, {new_y}, shift=False)'
    )
    report["preview"] = preview_result

    preview_bounds = None
    for _ in range(30):
        preview_bounds = _text_bounds(client, SPEAKER)
        if _within_one_pixel(preview_bounds, expected_screen_position):
            break
        time.sleep(0.1)
    say_after_preview = client.inspect_screen("say")
    say_scope_after = {
        key: (say_after_preview.get("scope") or {}).get(key)
        for key in ("who", "what")
    }
    report["preview_bounds"] = preview_bounds
    report["preview_geometry_correct"] = _within_one_pixel(
        preview_bounds, expected_screen_position
    )
    report["preview_preserved_dialogue"] = bool(
        say_after_preview.get("active") is True
        and say_scope_after == say_scope_before
    )

    gui_source_after_preview = gui_path.read_bytes()
    report["preview_source_unchanged"] = gui_source_after_preview == gui_source_initial

    save_click = client.click_element(id="rf_save", screen="_renforge_editor_overlay")
    report["save_click"] = save_click

    expected_committed = gui_source_initial.replace(
        b"define gui.name_xpos = gui.scale(240)",
        b"define gui.name_xpos = gui.scale(260)",
    ).replace(
        b"define gui.name_ypos = gui.scale(0)",
        b"define gui.name_ypos = gui.scale(30)",
    )
    last_status = _wait_for_transaction(client, gui_path, expected_committed)
    if not last_status or last_status.get("status_code") != "reload_committed":
        report["verdict"] = "fail"
        report["error"] = "commit timeout"
        report["last_status"] = last_status
        return report

    gui_source_after_commit = gui_path.read_bytes()
    report["gui_source_changed"] = gui_source_after_commit != gui_source_initial

    gui_text = gui_source_after_commit.decode("utf-8")
    xpos_patched = None
    ypos_patched = None
    for line in gui_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("define gui.name_xpos") and "=" in line:
            xpos_patched = int(line.split("(")[1].split(")")[0])
        if stripped.startswith("define gui.name_ypos") and "=" in line:
            ypos_patched = int(line.split("(")[1].split(")")[0])

    report["patched_xpos"] = xpos_patched
    report["patched_ypos"] = ypos_patched
    report["delta_correct"] = xpos_patched == 260 and ypos_patched == 30
    report["only_gui_position_changed"] = gui_source_after_commit == expected_committed
    report["screens_source_unchanged"] = fixture_path.read_bytes() == screens_source_initial

    client.eval_expr(
        f'renpy.show_screen("say", who="{SPEAKER}", what="Second line", _layer="screens")'
    )
    time.sleep(0.25)
    second_bounds = _text_bounds(client, SPEAKER)
    report["second_line_bounds"] = second_bounds
    report["committed_geometry_correct"] = _within_one_pixel(
        second_bounds, expected_screen_position
    )
    second_select = client.eval_expr(
        f'_renforge_editor_select({second_bounds["x"] + 10}, {second_bounds["y"] + 10})'
    ) if second_bounds else None
    report["second_line_select"] = second_select
    for _ in range(100):
        if client.request("editor_task0_status", {}).get("selected_lock_reason") != "ANALYZING":
            break
        time.sleep(0.1)
    if screenshot_path is not None and second_bounds:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot = client.screenshot()
        screenshot_path.write_bytes(screenshot)
        report["screenshot_path"] = str(screenshot_path)
        report["screenshot_sha256"] = hashlib.sha256(screenshot).hexdigest()

    undo_click = client.click_element(id="rf_undo", screen="_renforge_editor_overlay")
    report["undo_click"] = undo_click
    undo_status = _wait_for_transaction(client, gui_path, gui_source_initial)
    report["undo_status_code"] = undo_status.get("status_code") if undo_status else None
    gui_source_after_undo = gui_path.read_bytes()
    report["undo_byte_identical"] = gui_source_after_undo == gui_source_initial
    client.eval_expr(
        f'renpy.show_screen("say", who="{SPEAKER}", what="Undo line", _layer="screens")'
    )
    time.sleep(0.25)
    undo_bounds = _text_bounds(client, SPEAKER)
    report["undo_geometry_restored"] = _within_one_pixel(
        undo_bounds, [who_bounds["x"], who_bounds["y"]]
    )

    redo_click = client.click_element(id="rf_redo", screen="_renforge_editor_overlay")
    report["redo_click"] = redo_click
    redo_status = _wait_for_transaction(client, gui_path, gui_source_after_commit)
    report["redo_status_code"] = redo_status.get("status_code") if redo_status else None
    report["redo_byte_identical"] = gui_path.read_bytes() == gui_source_after_commit
    client.eval_expr(
        f'renpy.show_screen("say", who="{SPEAKER}", what="Redo line", _layer="screens")'
    )
    time.sleep(0.25)
    redo_bounds = _text_bounds(client, SPEAKER)
    report["redo_geometry_correct"] = _within_one_pixel(
        redo_bounds, expected_screen_position
    )

    report["verdict"] = (
        "pass"
        if move_unlocked
        and report.get("preview_geometry_correct")
        and report.get("preview_preserved_dialogue")
        and report.get("preview_source_unchanged")
        and report.get("delta_correct")
        and report.get("only_gui_position_changed")
        and report.get("screens_source_unchanged")
        and report.get("committed_geometry_correct")
        and report.get("undo_byte_identical")
        and report.get("undo_geometry_restored")
        and report.get("redo_byte_identical")
        and report.get("redo_geometry_correct")
        else "fail"
    )

    return report
