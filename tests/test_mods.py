import json
import zipfile
from pathlib import Path

import pytest

from renforge import mods, policy
from renforge.tools import mods as mod_tools
from renforge.state import project_state_dir


def _project(tmp_path: Path) -> Path:
    game = tmp_path / "game"
    game.mkdir()
    (game / "script.rpy").write_text("label start:\n    return\n", encoding="utf-8")
    return tmp_path


def _manifest(root: Path, name: str) -> dict:
    return json.loads((root / "game" / "mods" / name / "renforge-mod.json").read_text(encoding="utf-8"))


def test_write_creates_file_and_manifest(tmp_path) -> None:
    root = _project(tmp_path)

    result = mods.write_file(root, "demo", "scripts/intro.rpy", "label demo:\n    return\n")

    assert result["ok"] is True
    assert result["name"] == "demo"
    assert result["path"] == "game/mods/demo/scripts/intro.rpy"
    assert result["bytes"] == len("label demo:\n    return\n".encode("utf-8"))
    assert result["files"] == ["scripts/intro.rpy"]
    assert (root / "game" / "mods" / "demo" / "scripts" / "intro.rpy").read_text(encoding="utf-8") == (
        "label demo:\n    return\n"
    )
    manifest = _manifest(root, "demo")
    assert manifest["name"] == "demo"
    assert manifest["files"] == ["scripts/intro.rpy"]
    assert manifest["updated"].endswith("+00:00")
    assert "renforge-mod.json" not in manifest["files"]
    # The game's own script is never touched.
    assert (root / "game" / "script.rpy").read_text(encoding="utf-8") == "label start:\n    return\n"


def test_write_appends_to_manifest_and_overwrites_in_place(tmp_path) -> None:
    root = _project(tmp_path)
    mods.write_file(root, "demo", "b.rpy", "one")
    mods.write_file(root, "demo", "a.txt", "two")
    result = mods.write_file(root, "demo", "b.rpy", "three")

    assert result["files"] == ["a.txt", "b.rpy"]
    assert _manifest(root, "demo")["files"] == ["a.txt", "b.rpy"]
    assert (root / "game" / "mods" / "demo" / "b.rpy").read_text(encoding="utf-8") == "three"
    leftovers = [p.name for p in (root / "game" / "mods" / "demo").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


@pytest.mark.parametrize(
    "path",
    [
        "/abs/x.rpy",
        "../x.rpy",
        "a/../../x.rpy",
        "a\\b.rpy",
        "",
        "renforge-mod.json",
        "x.png",
        "x.exe",
        "x.rpy/",
        "./x.rpy",
        "C:/x.rpy",
    ],
)
def test_write_rejects_unsafe_paths(tmp_path, path) -> None:
    root = _project(tmp_path)

    with pytest.raises(ValueError):
        mods.write_file(root, "demo", path, "x")
    assert not (root / "game" / "mods").exists()


def test_write_rejects_oversize_content(tmp_path) -> None:
    root = _project(tmp_path)

    mods.write_file(root, "demo", "ok.rpy", "a" * mods.MAX_CONTENT_BYTES)
    with pytest.raises(ValueError, match="exceeds"):
        mods.write_file(root, "demo", "big.rpy", "a" * (mods.MAX_CONTENT_BYTES + 1))
    # Multi-byte characters count by UTF-8 bytes, not characters.
    with pytest.raises(ValueError):
        mods.write_file(root, "demo", "big2.rpy", "\u00e9" * (mods.MAX_CONTENT_BYTES // 2 + 1))


def test_write_rejects_symlinked_directory_under_game(tmp_path) -> None:
    root = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "game" / "mods").mkdir()
    (root / "game" / "mods" / "demo").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        mods.write_file(root, "demo", "x.rpy", "x")
    assert list(outside.iterdir()) == []


def test_write_rejects_symlinked_file_target(tmp_path) -> None:
    root = _project(tmp_path)
    mod_dir = root / "game" / "mods" / "demo"
    mod_dir.mkdir(parents=True)
    victim = root / "game" / "script.rpy"
    (mod_dir / "link.rpy").symlink_to(victim)

    with pytest.raises(ValueError, match="symlink"):
        mods.write_file(root, "demo", "link.rpy", "clobbered")
    assert victim.read_text(encoding="utf-8") == "label start:\n    return\n"


@pytest.mark.parametrize("name", ["Demo", "-demo", "", "a b", "a" * 65, "demo/../x", "demo\n"])
def test_bad_mod_name_is_rejected(tmp_path, name) -> None:
    root = _project(tmp_path)

    with pytest.raises(ValueError):
        mods.write_file(root, name, "x.rpy", "x")
    with pytest.raises(ValueError):
        mods.remove(root, name)


def test_list_reports_missing_and_unlisted(tmp_path) -> None:
    root = _project(tmp_path)
    mods.write_file(root, "demo", "a.rpy", "a")
    mods.write_file(root, "demo", "b.rpy", "b")
    mod_dir = root / "game" / "mods" / "demo"
    (mod_dir / "b.rpy").unlink()
    (mod_dir / "stray.txt").write_text("stray", encoding="utf-8")
    (mod_dir / "stray.rpyc").write_bytes(b"compiled")
    (root / "game" / "mods" / "unmanaged").mkdir()
    (root / "game" / "mods" / "unmanaged" / "x.rpy").write_text("x", encoding="utf-8")

    result = mods.list_mods(root)

    assert result["ok"] is True
    assert result["mods"] == [
        {"name": "demo", "files": ["a.rpy", "b.rpy"], "missing": ["b.rpy"], "unlisted": ["stray.txt"]}
    ]


def test_list_without_mods_dir_is_empty(tmp_path) -> None:
    root = _project(tmp_path)

    assert mods.list_mods(root) == {"ok": True, "mods": []}


def test_remove_deletes_listed_files_compiled_siblings_and_empty_dirs(tmp_path) -> None:
    root = _project(tmp_path)
    mods.write_file(root, "demo", "scripts/intro.rpy", "intro")
    mods.write_file(root, "demo", "notes.md", "notes")
    mod_dir = root / "game" / "mods" / "demo"
    (mod_dir / "scripts" / "intro.rpyc").write_bytes(b"compiled")
    (mod_dir / "scripts" / "unlisted.txt").write_text("keep me", encoding="utf-8")
    (mod_dir / "empty").mkdir()

    result = mods.remove(root, "demo")

    assert result["ok"] is True
    assert sorted(result["removed"]) == ["notes.md", "scripts/intro.rpy", "scripts/intro.rpyc"]
    assert result["kept"] == ["scripts/unlisted.txt"]
    assert (mod_dir / "scripts" / "unlisted.txt").read_text(encoding="utf-8") == "keep me"
    assert not (mod_dir / "scripts" / "intro.rpy").exists()
    assert not (mod_dir / "scripts" / "intro.rpyc").exists()
    assert not (mod_dir / "notes.md").exists()
    assert not (mod_dir / "empty").exists()
    assert not (mod_dir / "renforge-mod.json").exists()
    assert (root / "game" / "script.rpy").exists()


def test_remove_clears_mod_dir_when_nothing_unlisted(tmp_path) -> None:
    root = _project(tmp_path)
    mods.write_file(root, "demo", "scripts/intro.rpy", "intro")

    result = mods.remove(root, "demo")

    assert result == {"ok": True, "removed": ["scripts/intro.rpy"], "kept": []}
    assert not (root / "game" / "mods" / "demo").exists()
    assert (root / "game" / "mods").is_dir()


def test_remove_unknown_mod_is_an_error(tmp_path) -> None:
    root = _project(tmp_path)

    result = mods.remove(root, "ghost")

    assert result["ok"] is False
    assert "ghost" in result["error"]


def test_package_zips_listed_files_without_compiled_or_manifest(tmp_path) -> None:
    root = _project(tmp_path)
    mods.write_file(root, "demo", "scripts/intro.rpy", "intro")
    mods.write_file(root, "demo", "readme.md", "hello")
    (root / "game" / "mods" / "demo" / "scripts" / "intro.rpyc").write_bytes(b"compiled")

    result = mods.package(root, "demo")

    zip_path = project_state_dir(root) / "mods" / "demo.zip"
    assert result == {"ok": True, "path": str(zip_path), "files": ["readme.md", "scripts/intro.rpy"]}
    with zipfile.ZipFile(zip_path) as archive:
        assert sorted(archive.namelist()) == ["game/mods/demo/readme.md", "game/mods/demo/scripts/intro.rpy"]
        assert archive.read("game/mods/demo/readme.md") == b"hello"


def test_package_unknown_mod_is_an_error(tmp_path) -> None:
    root = _project(tmp_path)

    assert mods.package(root, "ghost")["ok"] is False


def test_lint_keeps_only_diagnostics_inside_the_mod(tmp_path) -> None:
    root = _project(tmp_path)
    seen = {}

    def fake_run(project_root):
        seen["root"] = project_root
        return {
            "ok": False,
            "returncode": 1,
            "diagnostics": [
                {"file": "game/mods/demo/a.rpy", "line": 3, "severity": "error", "message": "bad"},
                {"file": "mods/demo/b.rpy", "line": 1, "severity": "warning", "message": "meh"},
                {"file": "game\\mods\\demo\\c.rpy", "line": 2, "severity": "warning", "message": "win"},
                {"file": "game/script.rpy", "line": 9, "severity": "error", "message": "game"},
                {"file": "game/mods/demo2/d.rpy", "line": 1, "severity": "error", "message": "other"},
            ],
        }

    result = mods.lint(root, "demo", run=fake_run)

    assert seen["root"] == root
    assert result["ok"] is False
    assert result["returncode"] == 1
    assert [d["file"] for d in result["diagnostics"]] == [
        "game/mods/demo/a.rpy",
        "mods/demo/b.rpy",
        "game\\mods\\demo\\c.rpy",
    ]


def test_lint_is_ok_when_mod_has_no_diagnostics(tmp_path) -> None:
    root = _project(tmp_path)

    def clean_run(project_root):
        return {"ok": True, "returncode": 0, "diagnostics": [{"file": "game/script.rpy", "line": 1, "severity": "error"}]}

    assert mods.lint(root, "demo", run=clean_run) == {"ok": True, "diagnostics": [], "returncode": 0}


def test_lint_is_not_ok_when_lint_run_fails(tmp_path) -> None:
    root = _project(tmp_path)

    def failing_run(project_root):
        return {"ok": False, "error": "no sdk", "diagnostics": []}

    result = mods.lint(root, "demo", run=failing_run)

    assert result["ok"] is False
    assert result["error"] == "no sdk"


def test_guide_is_placeholder() -> None:
    assert "init offset = 999" in mods.GUIDE and "config.label_overrides" in mods.GUIDE


def test_dispatcher_guide_and_list(tmp_path) -> None:
    root = _project(tmp_path)

    assert mod_tools.mod(str(root), "guide") == {"ok": True, "guide": mods.GUIDE}
    assert mod_tools.mod(str(root), "list") == {"ok": True, "mods": []}


@pytest.mark.parametrize("action", ["write", "lint", "remove", "package"])
def test_dispatcher_requires_name(tmp_path, action) -> None:
    result = mod_tools.mod(str(tmp_path), action)

    assert result["ok"] is False
    assert "name" in result["error"]


def test_dispatcher_write_requires_path_and_content(tmp_path) -> None:
    root = _project(tmp_path)

    no_path = mod_tools.mod(str(root), "write", name="demo", content="x")
    no_content = mod_tools.mod(str(root), "write", name="demo", path="x.rpy")

    assert no_path["ok"] is False and "path" in no_path["error"]
    assert no_content["ok"] is False and "content" in no_content["error"]
    assert not (root / "game" / "mods").exists()


def test_dispatcher_rejects_unknown_action(tmp_path) -> None:
    result = mod_tools.mod(str(tmp_path), "explode", name="demo")

    assert result["ok"] is False
    assert "action" in result["error"]


def test_dispatcher_turns_value_errors_into_error_replies(tmp_path) -> None:
    root = _project(tmp_path)

    result = mod_tools.mod(str(root), "write", name="Bad Name", path="x.rpy", content="x")

    assert result["ok"] is False
    assert "mod name" in result["error"]


def test_dispatcher_write_then_remove(tmp_path) -> None:
    root = _project(tmp_path)

    written = mod_tools.mod(str(root), "write", name="demo", path="x.rpy", content="x")
    removed = mod_tools.mod(str(root), "remove", name="demo")

    assert written["ok"] is True
    assert removed == {"ok": True, "removed": ["x.rpy"], "kept": []}


def test_dispatcher_package_and_lint_route_to_mods(tmp_path, monkeypatch) -> None:
    root = _project(tmp_path)
    mod_tools.mod(str(root), "write", name="demo", path="x.rpy", content="x")
    calls = []

    def fake_lint(project_root, name, run=None):
        calls.append((project_root, name))
        return {"ok": True, "diagnostics": [], "returncode": 0}

    monkeypatch.setattr(mods, "lint", fake_lint)

    packaged = mod_tools.mod(str(root), "package", name="demo")
    linted = mod_tools.mod(str(root), "lint", name="demo")

    assert packaged["ok"] is True and packaged["files"] == ["x.rpy"]
    assert calls == [(str(root), "demo")]
    assert linted == {"ok": True, "diagnostics": [], "returncode": 0}


class _Reload:
    def __init__(self, observations, error_replies):
        self.observations = list(observations)
        self.error_replies = list(error_replies)
        self.controls = []
        self.observe_calls = 0
        self.error_calls = []

    def control(self, project_path, action, **kwargs):
        self.controls.append((project_path, action))
        return {"ok": True, "action": action}

    def observe(self, project_path, *, screenshot=True):
        assert screenshot is False
        self.observe_calls += 1
        return self.observations.pop(0)

    def get_errors(self, project_path, since=0):
        self.error_calls.append(since)
        return self.error_replies.pop(0)


def _install_reload(monkeypatch, fake: _Reload) -> None:
    monkeypatch.setattr(mod_tools, "control", fake.control)
    monkeypatch.setattr(mod_tools, "observe", fake.observe)
    monkeypatch.setattr(mod_tools, "get_errors", fake.get_errors)
    monkeypatch.setattr(mod_tools.time, "sleep", lambda seconds: None)


def test_reload_polls_until_stable_and_reports_clean_errors(tmp_path, monkeypatch) -> None:
    observation = {"ok": True, "stable": True, "dialogue": None}
    fake = _Reload(
        observations=[{"ok": True, "stable": False}, observation],
        error_replies=[
            {"ok": True, "events": [], "cursor": 4},
            {"ok": True, "events": []},
        ],
    )
    _install_reload(monkeypatch, fake)

    result = mod_tools.mod(str(tmp_path), "reload")

    assert fake.controls == [(str(tmp_path), "reload_script")]
    assert fake.observe_calls == 2
    assert fake.error_calls == [0, 4]
    assert result == {
        "ok": True,
        "reload": {"ok": True, "action": "reload_script"},
        "observation": observation,
        "errors": {"ok": True, "events": []},
    }


def test_reload_fails_when_new_errors_appear(tmp_path, monkeypatch) -> None:
    error_event = {"type": "exception", "message": "NameError"}
    fake = _Reload(
        observations=[{"ok": True, "stable": True}],
        error_replies=[{"ok": True, "events": [], "cursor": 2}, {"ok": True, "events": [error_event]}],
    )
    _install_reload(monkeypatch, fake)

    result = mod_tools.mod(str(tmp_path), "reload")

    assert result["ok"] is False
    assert result["errors"]["events"] == [error_event]


def test_reload_gives_up_after_timeout(tmp_path, monkeypatch) -> None:
    unstable = {"ok": True, "stable": False}
    fake = _Reload(
        observations=[unstable] * 5,
        error_replies=[{"ok": True, "events": []}, {"ok": True, "events": []}],
    )
    _install_reload(monkeypatch, fake)
    monkeypatch.setattr(mod_tools, "RELOAD_TIMEOUT", 0.0)

    result = mod_tools.mod(str(tmp_path), "reload")

    assert result["observation"] == unstable
    assert fake.observe_calls == 1
    assert result["ok"] is True


def test_reload_skips_polling_when_control_fails(tmp_path, monkeypatch) -> None:
    fake = _Reload(observations=[], error_replies=[{"ok": True, "events": []}])
    monkeypatch.setattr(mod_tools, "get_errors", fake.get_errors)
    monkeypatch.setattr(mod_tools, "observe", fake.observe)
    monkeypatch.setattr(
        mod_tools, "control", lambda project_path, action, **kwargs: {"ok": False, "error": "no running game"}
    )

    result = mod_tools.mod(str(tmp_path), "reload")

    assert result["ok"] is False
    assert result["reload"] == {"ok": False, "error": "no running game"}
    assert result["observation"] is None
    assert fake.observe_calls == 0


@pytest.mark.parametrize(
    ("action", "risk"),
    [
        ("write", policy.RISK_OPEN_WORLD),
        ("reload", policy.RISK_DESTRUCTIVE),
        ("remove", policy.RISK_MUTATING),
        ("list", policy.RISK_OBSERVATIONAL),
        ("lint", policy.RISK_OBSERVATIONAL),
        ("guide", policy.RISK_OBSERVATIONAL),
        ("package", policy.RISK_OBSERVATIONAL),
    ],
)
def test_policy_classifies_mod_actions(action, risk) -> None:
    assert policy.classify("renforge_mod", {"action": action}) == (f"renforge_mod.{action}", risk)


def test_policy_rejects_unknown_mod_action() -> None:
    assert policy.classify("renforge_mod", {"action": "explode"}) == (
        "renforge_mod.explode",
        policy.RISK_MALFORMED,
    )
    assert policy.classify("renforge_mod", {})[1] == policy.RISK_MALFORMED
