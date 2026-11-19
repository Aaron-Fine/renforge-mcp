import json
import zipfile
from pathlib import Path

import pytest

from renforge import mods, policy
from renforge.util import files as util_files
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

    assert result == {
        "ok": True,
        "removed": ["scripts/intro.rpy"],
        "kept": [],
        "files_deleted": ["game/mods/demo/renforge-mod.json", "game/mods/demo/scripts/intro.rpy"],
    }
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
    assert result == {
        "ok": True,
        "path": str(zip_path),
        "files": ["readme.md", "scripts/intro.rpy"],
        "files_touched": [str(zip_path)],
    }
    with zipfile.ZipFile(zip_path) as archive:
        assert sorted(archive.namelist()) == ["game/mods/demo/readme.md", "game/mods/demo/scripts/intro.rpy"]
        assert archive.read("game/mods/demo/readme.md") == b"hello"


def test_package_unknown_mod_is_an_error(tmp_path) -> None:
    root = _project(tmp_path)

    assert mods.package(root, "ghost")["ok"] is False


def test_lint_keeps_only_diagnostics_inside_the_mod(tmp_path) -> None:
    root = _project(tmp_path)
    mods.write_file(root, "demo", "a.rpy", "label a:\n    return\n")
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
    mods.write_file(root, "demo", "a.rpy", "label a:\n    return\n")

    def clean_run(project_root):
        return {"ok": True, "returncode": 0, "diagnostics": [{"file": "game/script.rpy", "line": 1, "severity": "error"}]}

    assert mods.lint(root, "demo", run=clean_run) == {"ok": True, "diagnostics": [], "returncode": 0}


def test_lint_is_not_ok_when_lint_run_fails(tmp_path) -> None:
    root = _project(tmp_path)
    mods.write_file(root, "demo", "a.rpy", "label a:\n    return\n")

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
    assert removed == {
        "ok": True,
        "removed": ["x.rpy"],
        "kept": [],
        "files_deleted": ["game/mods/demo/renforge-mod.json", "game/mods/demo/x.rpy"],
    }


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
    observation = {"ok": True, "stable": True, "dialogue": "Hello"}
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


def test_write_never_leaves_an_unlisted_file_when_the_manifest_fails(tmp_path, monkeypatch) -> None:
    # An unlisted .rpy would load in the game and survive remove().
    root = _project(tmp_path)

    def manifest_refused(*_args, **_kwargs):
        raise ValueError("refusing unsafe write to renforge-mod.json")

    monkeypatch.setattr(mods, "_write_manifest", manifest_refused)
    with pytest.raises(ValueError):
        mods.write_file(root, "demo", "intro.rpy", "label demo:\n    return\n")
    assert not (root / "game" / "mods" / "demo" / "intro.rpy").exists()


def test_package_reports_missing_files_and_fails_when_nothing_is_left(tmp_path) -> None:
    root = _project(tmp_path)
    mods.write_file(root, "demo", "a.rpy", "label a:\n    return\n")
    mods.write_file(root, "demo", "b.rpy", "label b:\n    return\n")
    (root / "game" / "mods" / "demo" / "b.rpy").unlink()

    partial = mods.package(root, "demo")
    assert partial["ok"] is True
    assert partial["files"] == ["a.rpy"]
    assert partial["missing"] == ["b.rpy"]

    (root / "game" / "mods" / "demo" / "a.rpy").unlink()
    empty = mods.package(root, "demo")
    assert empty["ok"] is False
    assert empty["missing"] == ["a.rpy", "b.rpy"]


def test_write_and_remove_work_through_a_symlinked_project_root(tmp_path) -> None:
    # A symlink above the project (a linked games library, macOS /var, or
    # /home -> /var/home) is not under game/ and must not block writes.
    real = tmp_path / "real"
    real.mkdir()
    _project(real)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    written = mods.write_file(link, "demo", "main.rpy", "label demo:\n    return\n")
    removed = mods.remove(link, "demo")

    assert written["ok"] is True
    assert removed["removed"] == ["main.rpy"]
    assert not (real / "game" / "mods" / "demo").exists()


def test_write_refuses_to_take_over_a_file_it_did_not_create(tmp_path) -> None:
    # A game with its own mod loader may ship game/mods/<name>/ files.
    root = _project(tmp_path)
    shipped = root / "game" / "mods" / "core" / "script.rpy"
    shipped.parent.mkdir(parents=True)
    shipped.write_text("label core:\n    return\n", encoding="utf-8")

    with pytest.raises(ValueError, match="another mod name"):
        mods.write_file(root, "core", "script.rpy", "clobbered")
    with pytest.raises(ValueError, match="another mod name"):
        mods.write_file(root, "core", "new.rpy", "label new:\n    return\n")
    assert shipped.read_text(encoding="utf-8") == "label core:\n    return\n"
    assert not (shipped.parent / "renforge-mod.json").exists()
    assert not (shipped.parent / "new.rpy").exists()


def test_write_refuses_an_unlisted_file_or_compiled_sibling_in_its_own_mod(tmp_path) -> None:
    root = _project(tmp_path)
    mods.write_file(root, "demo", "a.rpy", "a")
    mod_dir = root / "game" / "mods" / "demo"
    (mod_dir / "stray.rpy").write_text("stray", encoding="utf-8")
    (mod_dir / "only.rpyc").write_bytes(b"compiled")

    with pytest.raises(ValueError, match="did not write"):
        mods.write_file(root, "demo", "stray.rpy", "clobbered")
    # remove() deletes a listed .rpy's .rpyc, so a foreign .rpyc blocks the name too.
    with pytest.raises(ValueError, match="did not write"):
        mods.write_file(root, "demo", "only.rpy", "x")
    assert (mod_dir / "stray.rpy").read_text(encoding="utf-8") == "stray"
    assert (mod_dir / "only.rpyc").read_bytes() == b"compiled"
    assert _manifest(root, "demo")["files"] == ["a.rpy"]
    # Rewriting a file the mod already lists still works, even once compiled.
    (mod_dir / "a.rpyc").write_bytes(b"compiled")
    assert mods.write_file(root, "demo", "a.rpy", "b")["ok"] is True


def test_write_remove_and_package_return_write_receipts(tmp_path) -> None:
    root = _project(tmp_path)

    written = mods.write_file(root, "demo", "scripts/intro.rpy", "intro")
    packaged = mods.package(root, "demo")
    (root / "game" / "mods" / "demo" / "scripts" / "intro.rpyc").write_bytes(b"compiled")
    removed = mods.remove(root, "demo")

    assert written["files_touched"] == [
        "game/mods/demo/renforge-mod.json",
        "game/mods/demo/scripts/intro.rpy",
    ]
    assert packaged["files_touched"] == [str(project_state_dir(root) / "mods" / "demo.zip")]
    assert removed["files_deleted"] == [
        "game/mods/demo/renforge-mod.json",
        "game/mods/demo/scripts/intro.rpy",
        "game/mods/demo/scripts/intro.rpyc",
    ]


@pytest.mark.parametrize("path", ["Renforge-Mod.json", "RENFORGE-MOD.JSON", "sub/renforge-mod.json"])
def test_write_rejects_the_manifest_name_in_any_case_or_folder(tmp_path, path) -> None:
    # NTFS and APFS fold case, so a different spelling would replace the manifest.
    root = _project(tmp_path)

    with pytest.raises(ValueError, match="managed by RenForge"):
        mods.write_file(root, "demo", path, "{}")


def test_package_skips_an_invalid_manifest_entry_like_list_and_remove(tmp_path) -> None:
    root = _project(tmp_path)
    mods.write_file(root, "demo", "a.rpy", "label a:\n    return\n")
    manifest_path = root / "game" / "mods" / "demo" / "renforge-mod.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append("notes.png")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = mods.package(root, "demo")

    assert result["ok"] is True
    assert result["files"] == ["a.rpy"]
    assert result["missing"] == ["notes.png"]


def test_remove_uses_the_reparse_aware_link_check(tmp_path, monkeypatch) -> None:
    # Path.is_symlink() misses NTFS junctions; the write path's check does not.
    # Stand in for a junction: a directory the shared check reports as a link.
    root = _project(tmp_path)
    mods.write_file(root, "demo", "sub/a.rpy", "label a:\n    return\n")
    junction = (root / "game" / "mods" / "demo" / "sub").resolve()
    real_check = util_files._component_is_link
    monkeypatch.setattr(
        mods, "_component_is_link", lambda path: Path(path) == junction or real_check(path), raising=False
    )

    result = mods.remove(root, "demo")

    assert result["removed"] == []
    assert result["kept"] == ["sub/a.rpy"]
    assert (junction / "a.rpy").exists()


_PARSE_ERROR_OUTPUT = """\
I'm sorry, but errors were detected in your script. Please correct the
errors listed below, and try again.

File "game/mods/hud/main.rpy", line 3: expected statement.
    foo bar
       ^

File "game/script.rpy", line 9: expected statement.
"""


def test_lint_reports_a_parse_error_in_the_mod(tmp_path) -> None:
    from renforge.lint import parse_lint_output

    root = _project(tmp_path)
    mods.write_file(root, "hud", "main.rpy", "foo bar\n")

    def parse_error_run(project_root):
        return {
            "ok": False,
            "returncode": 1,
            "timed_out": False,
            "diagnostics": parse_lint_output(_PARSE_ERROR_OUTPUT),
            "raw": _PARSE_ERROR_OUTPUT,
        }

    result = mods.lint(root, "hud", run=parse_error_run)

    assert result["ok"] is False
    assert result["diagnostics"] == [
        {"file": "game/mods/hud/main.rpy", "line": 3, "severity": "error", "message": "expected statement."}
    ]


def test_lint_passes_on_a_timeout_and_the_output_tail(tmp_path) -> None:
    root = _project(tmp_path)
    mods.write_file(root, "hud", "main.rpy", "label hud:\n    return\n")
    raw = "x" * 5000 + "the end"

    def timed_out_run(project_root):
        return {"ok": False, "returncode": None, "timed_out": True, "diagnostics": [], "raw": raw}

    result = mods.lint(root, "hud", run=timed_out_run)

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["raw_tail"].endswith("the end")
    assert len(result["raw_tail"]) == 2000


def test_lint_of_an_unknown_mod_is_an_error(tmp_path) -> None:
    root = _project(tmp_path)
    calls = []

    result = mods.lint(root, "ghost", run=lambda project_root: calls.append(project_root) or {"ok": True})

    assert result == {"ok": False, "error": "unknown mod: ghost"}
    assert calls == []


def test_lint_runs_on_a_shadow_copy_by_default(tmp_path, monkeypatch) -> None:
    # Linting in place compiles .rpyc and writes game/cache outside the mod.
    from renforge.tools import validate

    root = _project(tmp_path)
    mods.write_file(root, "hud", "main.rpy", "foo bar\n")
    seen = []

    def fake_validate(project_path, **kwargs):
        seen.append(project_path)
        return {"ok": False, "returncode": 1, "timed_out": False, "stdout": _PARSE_ERROR_OUTPUT, "stderr": ""}

    monkeypatch.setattr(validate, "validate_project", fake_validate)
    monkeypatch.setattr(
        "renforge.lint.run_lint", lambda *a, **k: pytest.fail("mod lint must not lint the project in place")
    )

    result = mods.lint(root, "hud")

    assert seen == [str(root)]
    assert [d["line"] for d in result["diagnostics"]] == [3]


def test_reload_returns_a_compact_observation_like_the_play_tools(tmp_path, monkeypatch) -> None:
    raw = {
        "ok": True,
        "stable": True,
        "observation_id": "o1",
        "dialogue": None,
        "keys": [],
        "frame": {"width": 1280, "session_id": "s"},
    }
    fake = _Reload(observations=[raw], error_replies=[{"ok": True, "events": []}, {"ok": True, "events": []}])
    _install_reload(monkeypatch, fake)

    result = mod_tools.mod(str(tmp_path), "reload")

    assert result["observation"] == {"ok": True, "stable": True, "observation_id": "o1", "frame": {"width": 1280}}
