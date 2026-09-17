"""Required filesystem and process checks for the strict-play environment."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import strict_play_environment as environment  # noqa: E402

REQUIRED = os.environ.get("RENFORGE_STRICT_PLAY_TESTS") == "1"
MISSING = environment.missing_requirements()
pytestmark = pytest.mark.skipif(
    not REQUIRED and bool(MISSING),
    reason=f"requires {', '.join(MISSING)}",
)


@pytest.fixture()
def tree(tmp_path: Path) -> environment.EnvironmentTree:
    value = environment.EnvironmentTree(root=tmp_path / "case")
    value.build()
    return value


@pytest.fixture()
def mounted(tree: environment.EnvironmentTree):
    environment.mount_overlay(tree)
    try:
        yield tree
    finally:
        environment.umount_overlay(tree)


def _run_guest(
    tree: environment.EnvironmentTree, script: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        environment.build_guest_argv(tree, ["/bin/bash", "-c", script]),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_backend_is_available() -> None:
    assert not MISSING, f"missing strict-play commands: {', '.join(MISSING)}"
    environment.probe_backend()


def test_guest_cannot_access_host_state(mounted: environment.EnvironmentTree) -> None:
    script = f"""
set -u
for target in '{mounted.normal_saves}' '{mounted.trusted_state}' \
              '{mounted.normal_saves}/normal_save_canary.save' \
              '{mounted.trusted_state}/launch.json' \
              "$HOME/.renpy" "$HOME/.config/renforge"; do
  if [ -e "$target" ]; then echo "EXPOSED:$target"; fi
done
cat '{mounted.normal_saves}/normal_save_canary.save' 2>/dev/null || true
echo "HOME_IS:$HOME"
"""
    result = _run_guest(mounted, script)
    assert result.returncode == 0, result.stderr
    assert "EXPOSED:" not in result.stdout
    assert "NORMAL-SAVE-CANARY" not in result.stdout
    assert "HOME_IS:/home/guest" in result.stdout


def test_project_writes_use_only_the_overlay(mounted: environment.EnvironmentTree) -> None:
    lower = mounted.lower_project / "game" / "script.rpy"
    before = lower.read_bytes()
    result = _run_guest(
        mounted,
        "echo changed > /project/game/script.rpy; "
        "echo new > /project/new.rpy; "
        "echo local > /project/game/local.save",
    )
    assert result.returncode == 0, result.stderr
    assert lower.read_bytes() == before
    assert (mounted.upper / "new.rpy").read_text().strip() == "new"
    assert (mounted.upper / "game" / "local.save").read_text().strip() == "local"
    assert not (mounted.lower_project / "new.rpy").exists()


def test_writes_land_in_designated_roots(mounted: environment.EnvironmentTree) -> None:
    result = _run_guest(
        mounted,
        """
set -eu
echo game > /project/game/saves/game.save
echo primary > "$RENPY_PATH_TO_SAVES/primary.save"
echo multi > "$RENPY_MULTIPERSISTENT/state"
echo config > "$XDG_CONFIG_HOME/config"
echo cache > "$XDG_CACHE_HOME/cache"
echo data > "$XDG_DATA_HOME/data"
echo temp > "$TMPDIR/temp"
echo home > "$HOME/home"
echo public > /run/renforge/bridge.json
chmod 600 /run/renforge/bridge.json
""",
    )
    assert result.returncode == 0, result.stderr
    expected = {
        mounted.profile / "game-saves" / "game.save": "game",
        mounted.profile / "primary-saves" / "primary.save": "primary",
        mounted.profile / "multipersistent" / "state": "multi",
        mounted.xdg_config / "config": "config",
        mounted.xdg_cache / "cache": "cache",
        mounted.xdg_data / "data": "data",
        mounted.tmp / "temp": "temp",
        mounted.home / "home": "home",
        mounted.guest_pub / "bridge.json": "public",
    }
    assert {path: path.read_text().strip() for path in expected} == expected
    assert not (mounted.upper / "game" / "saves" / "game.save").exists()
    assert (mounted.guest_pub / "bridge.json").stat().st_mode & 0o777 == 0o600


def test_system_mounts_are_read_only(mounted: environment.EnvironmentTree) -> None:
    result = _run_guest(
        mounted,
        "echo forbidden > /usr/renforge-write-test 2>/dev/null; test $? -ne 0",
    )
    assert result.returncode == 0, result.stderr


def _namespace_members(namespace_ids: set[str]) -> set[int]:
    members: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.readlink(entry / "ns" / "pid") in namespace_ids:
                members.add(int(entry.name))
        except (OSError, PermissionError):
            continue
    return members


def _descendant_namespaces(root_pid: int) -> set[str]:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            line = next(
                line for line in (entry / "status").read_text().splitlines() if line.startswith("PPid:")
            )
            parents[int(entry.name)] = int(line.split()[1])
        except (OSError, PermissionError, StopIteration, ValueError):
            continue

    def descends_from_root(pid: int) -> bool:
        for _ in range(64):
            if pid == root_pid:
                return True
            pid = parents.get(pid, 0)
            if not pid:
                return False
        return False

    host_namespace = os.readlink("/proc/self/ns/pid")
    namespaces: set[str] = set()
    for pid in parents:
        if not descends_from_root(pid):
            continue
        try:
            namespace = os.readlink(f"/proc/{pid}/ns/pid")
        except (OSError, PermissionError):
            continue
        if namespace != host_namespace:
            namespaces.add(namespace)
    return namespaces


def test_guest_descendants_die_with_namespace(mounted: environment.EnvironmentTree) -> None:
    command = environment.build_guest_argv(
        mounted,
        [
            "/bin/bash",
            "-c",
            "setsid sleep 300 & (setsid sleep 300 &) ; setsid sleep 300 & exec sleep 300",
        ],
    )
    process = subprocess.Popen(command)
    time.sleep(1)
    namespaces = _descendant_namespaces(process.pid)
    assert namespaces
    assert _namespace_members(namespaces)

    process.kill()
    process.wait(timeout=10)
    time.sleep(0.7)
    assert not _namespace_members(namespaces)


def test_command_uses_only_read_only_system_binds(tree: environment.EnvironmentTree) -> None:
    command = environment.build_guest_argv(tree, ["/bin/true"])
    for system_path in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(system_path).exists():
            index = command.index(system_path)
            assert command[index - 1] == "--ro-bind"
