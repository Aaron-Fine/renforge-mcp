#!/usr/bin/env python3
"""Test-only Linux sandbox helpers for disposable isolation contract tests.

This prototype is not a production launcher and is not used by MCP tools.
MCP/dashboard isolation is application-layer (`renforge.save_isolation`) and
must not depend on Bubblewrap or FUSE. Keep this helper behind
`RENFORGE_SANDBOX_TESTS=1` and `tests/test_sandbox_contract.py`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class SandboxTestTree:
    """Disposable filesystem layout for one isolation check."""

    root: Path
    lower_project: Path = field(init=False)
    upper: Path = field(init=False)
    work: Path = field(init=False)
    merged: Path = field(init=False)
    normal_saves: Path = field(init=False)  # canary: user's real save tree
    trusted_state: Path = field(init=False)  # canary: RenForge locks/launch/trace
    profile: Path = field(init=False)  # designated writable profile state
    home: Path = field(init=False)  # session-private disposable home
    xdg_config: Path = field(init=False)
    xdg_cache: Path = field(init=False)
    xdg_data: Path = field(init=False)
    tmp: Path = field(init=False)
    guest_pub: Path = field(init=False)  # guest-publication dir (/run/renforge)

    def __post_init__(self) -> None:
        r = self.root
        self.lower_project = r / "lower_project"
        self.upper = r / "session" / "upper"
        self.work = r / "session" / "work"
        self.merged = r / "session" / "merged"
        self.normal_saves = r / "canary_normal_saves"
        self.trusted_state = r / "canary_trusted_state"
        self.profile = r / "profile"
        self.home = r / "session" / "home"
        self.xdg_config = r / "session" / "xdg-config"
        self.xdg_cache = r / "session" / "xdg-cache"
        self.xdg_data = r / "session" / "xdg-data"
        self.tmp = r / "session" / "tmp"
        self.guest_pub = r / "session" / "guest_publication"

    def build(self) -> None:
        # Lower (host) project with a sentinel file and a game/saves dir.
        (self.lower_project / "game" / "saves").mkdir(parents=True)
        (self.lower_project / "game" / "script.rpy").write_text(
            "# lower sentinel\n"
        )
        (self.lower_project / "game" / "saves" / "lower_sentinel.save").write_text(
            "LOWER-SENTINEL\n"
        )
        # Canary trees that must be invisible / unreadable to the guest.
        self.normal_saves.mkdir(parents=True)
        (self.normal_saves / "normal_save_canary.save").write_text(
            "NORMAL-SAVE-CANARY\n"
        )
        self.trusted_state.mkdir(parents=True)
        (self.trusted_state / "launch.json").write_text('{"token":"TRUSTED"}\n')
        # Designated writable state.
        (self.profile / "primary-saves").mkdir(parents=True)
        (self.profile / "game-saves").mkdir(parents=True)
        (self.profile / "multipersistent").mkdir(parents=True)
        for d in (
            self.upper,
            self.work,
            self.merged,
            self.home,
            self.xdg_config,
            self.xdg_cache,
            self.xdg_data,
            self.tmp,
            self.guest_pub,
        ):
            d.mkdir(parents=True)


BWRAP = shutil.which("bwrap") or "bwrap"
FUSE_OVERLAYFS = shutil.which("fuse-overlayfs") or "fuse-overlayfs"
FUSERMOUNT = shutil.which("fusermount3") or "fusermount3"


def _system_binds() -> list[str]:
    """Minimal read-only system mounts for a usertools-capable namespace."""
    args: list[str] = []
    for p in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(p).exists():
            args += ["--ro-bind", p, p]
    return args


def missing_requirements() -> tuple[str, ...]:
    names = ("bwrap", "fuse-overlayfs", "fusermount3")
    missing = [name for name in names if shutil.which(name) is None]
    if not Path("/dev/fuse").exists():
        missing.append("/dev/fuse")
    return tuple(missing)


def probe_backend() -> None:
    """Fail unless this host can create the namespace used by the contract tests."""
    missing = missing_requirements()
    if missing:
        raise RuntimeError(f"Missing sandbox test prerequisites: {', '.join(missing)}")
    subprocess.run(
        [
            BWRAP,
            "--unshare-user",
            "--unshare-pid",
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--",
            "/usr/bin/true",
        ],
        check=True,
    )


def mount_overlay(tree: SandboxTestTree) -> None:
    """Host-side CoW overlay. No privilege required; caller owns all dirs."""
    subprocess.run(
        [
            FUSE_OVERLAYFS,
            "-o",
            f"lowerdir={tree.lower_project},upperdir={tree.upper},workdir={tree.work}",
            str(tree.merged),
        ],
        check=True,
    )


def umount_overlay(tree: SandboxTestTree) -> None:
    subprocess.run([FUSERMOUNT, "-u", str(tree.merged)], check=True)


def build_guest_argv(tree: SandboxTestTree, inner: list[str]) -> list[str]:
    """Construct the filesystem/PID boundary exercised by the contract tests.

    This prototype does not isolate networking or sanitize the inherited
    environment; do not use it to launch untrusted projects.

    The merged overlay is bound read-write at the logical project path. The
    profile's game-saves dir is bound over the runtime game/saves. Only the
    fresh guest-publication dir is exposed; normal saves, trusted state, and
    the broader home/workspace are simply never mounted (empty namespace).
    """
    project_path = "/project"
    argv = [
        BWRAP,
        "--unshare-user",
        "--unshare-pid",
        "--die-with-parent",
        "--proc",
        "/proc",
        "--dev-bind",
        "/dev/null",
        "/dev/null",
        "--dev-bind",
        "/dev/zero",
        "/dev/zero",
        "--dev-bind",
        "/dev/urandom",
        "/dev/urandom",
        # Project: merged CoW view, read-write to the guest.
        "--bind",
        str(tree.merged),
        project_path,
        # game/saves persisted into the profile (bind over the merged path).
        "--bind",
        str(tree.profile / "game-saves"),
        f"{project_path}/game/saves",
        "--bind",
        str(tree.profile / "primary-saves"),
        "/profile/primary-saves",
        "--bind",
        str(tree.profile / "multipersistent"),
        "/profile/multipersistent",
        # Fresh guest-publication dir for bridge control publication.
        "--bind",
        str(tree.guest_pub),
        "/run/renforge",
        # Session-private disposable home/tmp.
        "--bind",
        str(tree.home),
        "/home/guest",
        "--bind",
        str(tree.xdg_config),
        "/xdg/config",
        "--bind",
        str(tree.xdg_cache),
        "/xdg/cache",
        "--bind",
        str(tree.xdg_data),
        "/xdg/data",
        "--bind",
        str(tree.tmp),
        "/tmp",
        "--setenv",
        "HOME",
        "/home/guest",
        "--setenv",
        "XDG_CONFIG_HOME",
        "/xdg/config",
        "--setenv",
        "XDG_CACHE_HOME",
        "/xdg/cache",
        "--setenv",
        "XDG_DATA_HOME",
        "/xdg/data",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "RENPY_PATH_TO_SAVES",
        "/profile/primary-saves",
        "--setenv",
        "RENPY_MULTIPERSISTENT",
        "/profile/multipersistent",
        "--setenv",
        "RENFORGE_BRIDGE_PUBLICATION_DIR",
        "/run/renforge",
        "--chdir",
        project_path,
        "--",
    ]
    argv[1:1] = _system_binds()
    argv += inner
    return argv


def main() -> int:
    if sys.argv[1:] != ["--probe"]:
        print("usage: sandbox_test_support.py --probe", file=sys.stderr)
        return 2
    probe_backend()
    print("sandbox test namespace backend available")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
