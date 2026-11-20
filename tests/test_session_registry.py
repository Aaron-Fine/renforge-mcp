import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_windows_temp_registry_digest_is_process_stable() -> None:
    command = [
        sys.executable,
        "-c",
        (
            "from renforge.session_registry import _windows_user_digest; "
            "print(_windows_user_digest('same-user'))"
        ),
    ]
    outputs = []
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        outputs.append(
            subprocess.check_output(command, env=env, text=True).strip()
        )

    assert outputs[0] == outputs[1]
    assert len(outputs[0]) == 12


def test_dashboard_project_is_discoverable_across_processes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RENFORGE_RUNTIME_DIR", str(tmp_path / "runtime"))

    from renforge.session_registry import (
        active_dashboard,
        dashboard_connection,
        publish_dashboard,
    )

    project = tmp_path / "visual-novel"
    publish_dashboard(project, url="http://127.0.0.1:8765/", token="secret_token_22chars__")

    context = active_dashboard()
    assert context is not None
    assert context["project"] == str(project.resolve())
    assert context["url"] == "http://127.0.0.1:8765/"
    assert context["pid"] > 0
    assert "token" not in context
    assert dashboard_connection()["token"] == "secret_token_22chars__"


def test_dashboard_lifespan_publishes_and_clears_its_project(tmp_path: Path, monkeypatch) -> None:
    testclient = pytest.importorskip("starlette.testclient")
    monkeypatch.setenv("RENFORGE_RUNTIME_DIR", str(tmp_path / "runtime"))

    from renforge.session_registry import active_dashboard
    from renforge.ui.server import create_ui_app

    project = tmp_path / "visual-novel"
    (project / "game").mkdir(parents=True)
    app = create_ui_app(project, ui_token="secret_token_22chars__", dashboard_url="http://127.0.0.1:8765/")

    with testclient.TestClient(app):
        assert active_dashboard()["project"] == str(project.resolve())
        from renforge.session_registry import dashboard_connection

        assert dashboard_connection()["token"] == "secret_token_22chars__"

    assert active_dashboard() is None


def test_dashboard_project_switch_updates_the_shared_context(tmp_path: Path, monkeypatch) -> None:
    testclient = pytest.importorskip("starlette.testclient")
    monkeypatch.setenv("RENFORGE_RUNTIME_DIR", str(tmp_path / "runtime"))

    from renforge.session_registry import active_dashboard
    from renforge.ui.server import create_ui_app

    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "game").mkdir(parents=True)
    (second / "game").mkdir(parents=True)
    app = create_ui_app(first, ui_token="secret_token_22chars__", dashboard_url="http://127.0.0.1:8765/")

    with testclient.TestClient(app) as client:
        response = client.post(
            "/api/project?token=secret_token_22chars__",
            json={"root_id": "project-parent", "path": "second"},
        )
        assert response.status_code == 200
        assert active_dashboard()["project"] == str(second.resolve())


def test_dashboard_matches_every_spelling_of_its_project_on_a_case_insensitive_volume(
    tmp_path: Path, monkeypatch
) -> None:
    """The dashboard's project and a request's spelling of it share a state folder, so they match."""
    monkeypatch.setenv("RENFORGE_RUNTIME_DIR", str(tmp_path / "runtime"))
    from renforge import dashboard_client, state
    from renforge.session_registry import dashboard_connection, publish_dashboard

    project = tmp_path / "Game"
    (project / "game").mkdir(parents=True)
    other_spelling = tmp_path / "game"
    monkeypatch.setattr(state, "_same_file", lambda a, b: str(a).casefold() == str(b).casefold())
    publish_dashboard(project, url="http://127.0.0.1:8765/", token="secret_token_22chars__")

    connection = dashboard_connection(other_spelling)
    assert connection is not None and connection["project"] == str(project.resolve())

    # The request goes to that dashboard (here it cannot be reached), not to a local launch.
    from urllib.error import URLError

    def unreachable(request, timeout):
        raise URLError("unreachable")

    monkeypatch.setattr(dashboard_client, "urlopen", unreachable)
    result = dashboard_client._request(str(other_spelling), "status", operation="status", method="GET")
    assert result is not None and result["code"] == "DASHBOARD_REQUEST_FAILED"



def test_a_live_dashboard_stays_discoverable_after_a_case_only_rename(tmp_path: Path, monkeypatch) -> None:
    """A reader that starts after /Games/Foo became /Games/foo still finds the dashboard publishing /Games/Foo."""
    import os

    monkeypatch.setenv("RENFORGE_RUNTIME_DIR", str(tmp_path / "runtime"))
    from renforge import state
    from renforge.session_registry import dashboard_connection, publish_dashboard

    project = tmp_path / "Games" / "Foo"
    (project / "game").mkdir(parents=True)
    publish_dashboard(project, url="http://127.0.0.1:8765/", token="secret_token_22chars__")
    renamed = tmp_path / "Games" / "foo"
    project.rename(renamed)

    real = os.path.samefile

    def case_insensitive(a, b):
        a, b = str(a), str(b)
        if a.casefold() == b.casefold() and a.casefold().startswith(str(tmp_path).casefold()):
            return True
        return real(a, b)

    monkeypatch.setattr(state.os.path, "samefile", case_insensitive)
    # The reader is a separate process: it has none of the publisher's spellings.
    state._SPELLINGS.clear()
    state._SPELLINGS_BY_FOLD.clear()

    connection = dashboard_connection(renamed)
    assert connection is not None
    assert connection["project"] == str(renamed.resolve())

    # A record redirected to another directory is still refused.
    other = tmp_path / "Games" / "other"
    (other / "game").mkdir(parents=True)
    assert dashboard_connection(other) is None
