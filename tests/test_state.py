"""Per-project state folders: one folder, and one bridge lock, per real directory."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from renforge import state


@pytest.fixture(autouse=True)
def _state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(state.STATE_DIR_ENV, str(tmp_path / "state"))


def _pretend_case_insensitive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer as a case-insensitive volume would: two spellings under tmp_path are one folder."""
    real = os.path.samefile

    def case_insensitive(a, b):
        a, b = str(a), str(b)
        if a.casefold() == b.casefold() and a.casefold().startswith(str(tmp_path).casefold()):
            return True
        return real(a, b)

    monkeypatch.setattr(state.os.path, "samefile", case_insensitive)


def _new_process() -> None:
    """Forget every spelling, as a process started now would have none."""
    state._SPELLINGS.clear()
    state._SPELLINGS_BY_FOLD.clear()


def test_an_exact_spelling_keeps_its_existing_folder_name(tmp_path: Path) -> None:
    project = tmp_path / "Games" / "Foo"
    project.mkdir(parents=True)
    digest = hashlib.sha256(str(project.resolve()).encode("utf-8")).hexdigest()[:16]
    assert state.project_state_dir(project).name == f"Foo-{digest}"


def test_another_case_spelling_of_one_directory_shares_its_folder(tmp_path: Path, monkeypatch) -> None:
    """A case-insensitive volume (macOS) answers to any spelling of a name."""
    project = tmp_path / "Games" / "Foo"
    project.mkdir(parents=True)
    _pretend_case_insensitive(tmp_path, monkeypatch)
    assert state.project_state_dir(tmp_path / "games" / "FOO") == state.project_state_dir(project)
    assert state.control_dir(tmp_path / "GAMES" / "foo") == state.control_dir(project)


def test_case_sensitive_siblings_keep_separate_folders(tmp_path: Path) -> None:
    upper = tmp_path / "Game"
    lower = tmp_path / "game"
    upper.mkdir()
    if lower.exists():
        pytest.skip("this volume ignores case")
    lower.mkdir()
    assert state.project_state_dir(upper) != state.project_state_dir(lower)


def test_a_repeated_lookup_does_not_list_every_ancestor_again(tmp_path: Path, monkeypatch) -> None:
    """Hot paths (every observe poll, every activity entry) ask for the folder again."""
    project = tmp_path / "Games" / "Foo"
    project.mkdir(parents=True)
    first = state.project_state_dir(project)
    listed: list[str] = []
    real = os.listdir

    def counting(path):
        listed.append(str(path))
        return real(path)

    monkeypatch.setattr(state.os, "listdir", counting)
    assert state.project_state_dir(project) == first
    assert state.control_dir(project) == first / "control"
    assert listed == []


def test_a_path_spelled_before_it_existed_is_spelled_again(tmp_path: Path, monkeypatch) -> None:
    """A missing folder cannot be spelled from its listing, so its answer is not kept."""
    project = tmp_path / "Later"
    before = state.project_state_dir(project)
    project.mkdir()
    listed: list[str] = []
    real = os.listdir

    def counting(path):
        listed.append(str(path))
        return real(path)

    monkeypatch.setattr(state.os, "listdir", counting)
    assert state.project_state_dir(project) == before
    assert listed


def test_a_case_only_rename_keeps_this_process_on_its_folder(tmp_path: Path, monkeypatch) -> None:
    """A server running a game keeps that game's lock and metadata after the folder is renamed."""
    project = tmp_path / "Games" / "Foo"
    project.mkdir(parents=True)
    first = state.project_state_dir(project)
    project.rename(tmp_path / "Games" / "foo")
    _pretend_case_insensitive(tmp_path, monkeypatch)
    assert state.project_state_dir(project) == first
    _new_process()  # a process started after the rename
    assert state.project_state_dir(project) != first


def test_a_case_only_rename_keeps_this_process_on_its_folder_when_resolve_respells_it(
    tmp_path: Path, monkeypatch
) -> None:
    """On Windows resolve() returns the disk's spelling, so after the rename every lookup asks for the new one."""
    project = tmp_path / "Games" / "Foo"
    project.mkdir(parents=True)
    first = state.project_state_dir(project)
    renamed = tmp_path / "Games" / "foo"
    project.rename(renamed)
    _pretend_case_insensitive(tmp_path, monkeypatch)
    assert state.project_state_dir(renamed) == first
    assert state.control_dir(renamed) == first / "control"
    _new_process()
    assert state.project_state_dir(renamed) != first


def test_a_running_sessions_spelling_survives_the_cache_filling_up(tmp_path: Path, monkeypatch) -> None:
    """Eviction must not move a running session off its lock and metadata after a case-only rename."""
    import gc

    project = tmp_path / "Games" / "Foo"
    project.mkdir(parents=True)
    first = state.project_state_dir(project)

    class _Session:
        pass

    session = _Session()
    state.pin_project_spelling(project, owner=session)
    renamed = tmp_path / "Games" / "foo"
    project.rename(renamed)
    _pretend_case_insensitive(tmp_path, monkeypatch)
    for n in range(state._SPELLINGS_MAX + 1):
        state._keep(f"/unrelated/{n}", f"/unrelated/{n}", Path(f"/unrelated/{n}"))

    assert state.project_state_dir(project) == first
    assert state.project_state_dir(renamed) == first

    # Once the session is gone its spelling is an ordinary cache entry again.
    del session
    gc.collect()
    for n in range(state._SPELLINGS_MAX + 1):
        state._keep(f"/other/{n}", f"/other/{n}", Path(f"/other/{n}"))
    assert state.project_state_dir(renamed) != first


def test_activity_recorded_under_the_earlier_state_key_stays_readable(tmp_path: Path, monkeypatch) -> None:
    """Before the key spelled paths as the disk does, `games/foo` had its own folder for `Games/Foo`."""
    import json

    from renforge.ui.activity import read_recent_activity

    project = tmp_path / "Games" / "Foo"
    project.mkdir(parents=True)
    _pretend_case_insensitive(tmp_path, monkeypatch)
    spelled = tmp_path / "games" / "foo"
    resolved = spelled.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    earlier = state.state_root() / "projects" / f"foo-{digest}"
    assert earlier != state.project_state_dir(spelled)
    earlier.mkdir(parents=True)
    (earlier / "activity.jsonl").write_text(json.dumps({"ts": 1, "tool": "old"}) + "\n", encoding="utf-8")
    current = state.project_state_dir(spelled)
    current.mkdir(parents=True)
    (current / "activity.jsonl").write_text(json.dumps({"ts": 2, "tool": "new"}) + "\n", encoding="utf-8")

    tools = [event["payload"]["tool"] for event in read_recent_activity(spelled)]
    assert tools == ["old", "new"]
    # The disk's own spelling had no other folder, so it has nothing earlier to read.
    assert state.prior_project_state_dir(project) is None
    assert [event["payload"]["tool"] for event in read_recent_activity(project)] == ["new"]
