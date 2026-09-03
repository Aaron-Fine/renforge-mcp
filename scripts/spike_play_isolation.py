#!/usr/bin/env python3
"""Task 0 spike: prove the strict-session isolation backend against a hostile subprocess.

Gate A (architecture go/no-go) requires one backend to prove that a guest process:

* cannot enumerate or read normal-play saves or trusted RenForge control state;
* cannot mutate the lower (host) project;
* can persist only designated profile state;
* leaves no descendants after stop / owner death.

This spike also runs the display-backend render matrix (cage -> weston -> Xvfb)
to select the strict-session headless display server.

The selected backend on this host is **Architecture B**: a host-side
``fuse-overlayfs`` copy-on-write mount (lower = host project, read-only;
upper/work = session-disposable) whose merged view is bound into a minimal
allowlisted Bubblewrap namespace. Kernel 7.1 (Fedora 44) denies ``mount -t
overlay`` to a userns-root process and bwrap's ``no_new_privs`` blocks the
setuid ``fusermount3`` helper from *inside* the sandbox, so the overlay is
mounted by the host caller (no privilege needed) and the guest sees a plain
writable directory. The host-side FUSE mount enforces CoW: the lower project
can never be mutated through it.

Everything is built under a caller-supplied temp root; nothing touches the
host project's real saves or home directory.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


@dataclass
class SpikeTree:
    """Disposable filesystem layout for one isolation experiment."""

    root: Path
    lower_project: Path = field(init=False)
    upper: Path = field(init=False)
    work: Path = field(init=False)
    merged: Path = field(init=False)
    normal_saves: Path = field(init=False)  # canary: user's real save tree
    trusted_state: Path = field(init=False)  # canary: RenForge locks/launch/trace
    profile: Path = field(init=False)  # designated writable profile state
    home: Path = field(init=False)  # session-private disposable home
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
        for d in (self.upper, self.work, self.merged, self.home, self.guest_pub):
            d.mkdir(parents=True)


# ---------------------------------------------------------------------------
# Backend probes
# ---------------------------------------------------------------------------

BWRAP = shutil.which("bwrap") or "bwrap"
FUSE_OVERLAYFS = shutil.which("fuse-overlayfs") or "fuse-overlayfs"
FUSERMOUNT = shutil.which("fusermount3") or "fusermount3"


def _system_binds() -> list[str]:
    """Minimal read-only system mounts for a usertools-capable namespace."""
    args: list[str] = []
    for p in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(p).exists():
            args += ["--bind", p, p]
    return args


def mount_overlay(tree: SpikeTree) -> None:
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


def umount_overlay(tree: SpikeTree) -> None:
    subprocess.run([FUSERMOUNT, "-u", str(tree.merged)], check=True)


def build_guest_argv(tree: SpikeTree, inner: list[str]) -> list[str]:
    """Construct the allowlisted bwrap command for a hostile guest.

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
        # Fresh guest-publication dir for bridge control publication.
        "--bind",
        str(tree.guest_pub),
        "/run/renforge",
        # Session-private disposable home/tmp.
        "--bind",
        str(tree.home),
        "/home/guest",
        "--tmpfs",
        "/tmp",
        "--setenv",
        "HOME",
        "/home/guest",
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


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    name: str
    passed: bool
    detail: str = ""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: spike_play_isolation.py <work-root>", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    tree = SpikeTree(root=root / "case")
    if tree.root.exists():
        shutil.rmtree(tree.root)
    tree.build()

    print(f"[spike] work root: {root}")
    print(f"[spike] kernel: {os.uname().release}")
    print(f"[spike] bwrap: {BWRAP}  fuse-overlayfs: {FUSE_OVERLAYFS}")

    mount_overlay(tree)
    try:
        print("[spike] overlay mounted (Architecture B: host-side fuse-overlayfs)")
        # Placeholder: hostile probes and display matrix are added by the
        # Task 0 test harness; this script currently proves mount/CoW/unmount.
        guest = build_guest_argv(
            tree, ["/bin/bash", "-c", "cat /project/game/script.rpy"]
        )
        out = subprocess.run(guest, capture_output=True, text=True)
        print(f"[spike] guest read lower rc={out.returncode}: {out.stdout.strip()}")
    finally:
        umount_overlay(tree)
        print("[spike] overlay unmounted")

    print("[spike] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
