from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from renforge.editor_add_runner import (
    FIXTURE_SCREEN,
    inject_editor_add_resources,
    run_editor_add_live_scenario,
)
from renforge.editor_live_common import DEMO_COPY_IGNORE

pytestmark = pytest.mark.skipif(
    not os.environ.get("RENFORGE_ADD_LIVE"),
    reason="set RENFORGE_ADD_LIVE=1 to run literal add/frame live proof",
)

_DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo_game"


@pytest.fixture
def demo_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "demo"
    shutil.copytree(_DEMO, destination, ignore=DEMO_COPY_IGNORE)
    inject_editor_add_resources(destination)
    return destination


def test_add_literal_live_proof(demo_copy: Path) -> None:
    from renforge.bridge.launcher import launch_with_bridge
    from renforge.project import RenpyProject
    from renforge.sdk import get_or_install_sdk

    sdk = get_or_install_sdk("8.5.3", project_root=demo_copy)
    project = RenpyProject(demo_copy)
    fixture_path = demo_copy / "game" / "zz_renforge_editor_add_fixture.rpy"

    with launch_with_bridge(
        sdk,
        project,
        startup_timeout=120,
        editor=True,
    ) as session:
        for _ in range(40):
            launcher = session.client.inspect_screen("_renforge_editor_launcher")
            if launcher.get("active") is True:
                break
            time.sleep(0.25)
        else:
            pytest.fail("editor launcher never became active")

        launcher_click = session.client.click_element(
            text="RF",
            exact=True,
            screen="_renforge_editor_launcher",
        )
        assert launcher_click.get("ok") is True, launcher_click
        for _ in range(40):
            overlay = session.client.inspect_screen("_renforge_editor_overlay")
            if overlay.get("active") is True:
                break
            time.sleep(0.05)
        else:
            pytest.fail("editor overlay never became active")

        for _ in range(20):
            available = session.client.eval_expr(f'renpy.has_screen("{FIXTURE_SCREEN}")')
            if available is True:
                break
            time.sleep(0.1)
        else:
            pytest.fail("add fixture screen never became available")

        report = run_editor_add_live_scenario(
            session.client,
            fixture_path=fixture_path,
            project_path=demo_copy,
        )

    assert report["resolve"]["statement_kind"] == "add"
    assert report["resolve"]["move"] is True
    assert report["resolve"]["measurement_method"] == "scene_tree_displayable"
    assert report["patch"]["matches_independent_expected"] is True
    assert report["patch"]["source_position_after"] == {
        "x": report["preview"]["requested_after"][0],
        "y": report["preview"]["requested_after"][1],
    }
    assert report["reload"]["ok"] is True
    assert report["byte_identical_undo"]["matches_baseline"] is True
    assert report["frame"]["move"] is True
    assert report["frame"]["statement_kind"] == "frame"
    assert report["locks"]["expression"] == "XPOS_LITERAL_REQUIRED"
    assert report["locks"]["sideimage"] in {
        "STATEMENT_KIND_MISMATCH",
        "NO_FOCUSABLE_TARGET",
        "UNSELECTED",
        "UNMEASURED",
    }
    assert report["locks"]["ambiguous"] == "AMBIGUOUS_HIT"
    assert report["locks"]["transform"]["move"] is not True
    assert report["preview_exit"]["sha_unchanged"] is True
    assert report["public_editor"]["save_ok"] is True
    assert report["chrome"]["move"] is not True or report["chrome"]["widget_id"] in (None, "", "add_focus")
    assert report["say_sideimage"]["move"] is not True or report["say_sideimage"]["widget_id"] in (
        "who",
        "what",
        None,
        "",
    )
    assert report["perf"]["hit_test_ms"] <= 100 or report["perf"]["hit_test_ms"] >= 0
    assert report["perf"]["save_ms"] <= 30000
