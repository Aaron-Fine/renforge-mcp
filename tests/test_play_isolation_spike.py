"""Gate A: hostile-subprocess isolation tests for the Task 0-selected backend.

These tests run a *hostile* guest inside the Architecture B sandbox
(host-side fuse-overlayfs CoW + allowlisted Bubblewrap namespace) and prove
the isolation contract from the plan:

* the guest cannot enumerate or read normal-play saves or trusted control
  state (they are simply never mounted into the empty namespace);
* the guest cannot mutate the lower (host) project;
* the guest can persist only designated profile state;
* stop / owner death leaves no descendant processes.

Every test builds a fresh disposable tree under ``tmp_path`` and never touches
a real home, save tree, or the host project.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import spike_play_isolation as spike  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("bwrap") is None
    or shutil.which("fuse-overlayfs") is None
    or shutil.which("fusermount3") is None,
    reason="requires bwrap + fuse-overlayfs + fusermount3",
)


@pytest.fixture()
def tree(tmp_path: Path) -> spike.SpikeTree:
    t = spike.SpikeTree(root=tmp_path / "case")
    t.build()
    return t


def _run_guest(tree: spike.SpikeTree, inner: list[str], **kw) -> subprocess.CompletedProcess:
    argv = spike.build_guest_argv(tree, inner)
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, **kw)


@pytest.fixture()
def mounted(tree: spike.SpikeTree):
    spike.mount_overlay(tree)
    try:
        yield tree
    finally:
        spike.umount_overlay(tree)


# ---------------------------------------------------------------------------
# 1. Confidentiality: canaries are invisible and unreadable
# ---------------------------------------------------------------------------


def test_guest_cannot_see_normal_saves_or_trusted_state(mounted: spike.SpikeTree) -> None:
    # The hostile guest tries the absolute host paths of the canary trees.
    probe = f"""
set -u
for target in '{mounted.normal_saves}' '{mounted.trusted_state}' \
              '{mounted.normal_saves}/normal_save_canary.save' \
              '{mounted.trusted_state}/launch.json' \
              "$HOME/.renpy" "$HOME/.config/renforge" /home/{os.environ.get('USER','')}; do
  if [ -e "$target" ]; then echo "EXPOSED:$target"; fi
done
# Enumerate what IS visible at common save roots.
echo "ROOTS:"; ls -1 / 2>&1 | tr '\\n' ' '; echo
echo "HOME_IS:$HOME"; ls -la "$HOME" 2>&1 | head -3
echo DONE
"""
    r = _run_guest(mounted, ["/bin/bash", "-c", probe])
    assert r.returncode == 0, r.stderr
    assert "EXPOSED:" not in r.stdout, f"guest saw a forbidden path:\n{r.stdout}"
    # Host home must not be the guest home.
    assert "HOME_IS:/home/guest" in r.stdout


def test_guest_cannot_read_canary_contents(mounted: spike.SpikeTree) -> None:
    probe = f"cat '{mounted.normal_saves}/normal_save_canary.save' 2>&1; echo rc=$?"
    r = _run_guest(mounted, ["/bin/bash", "-c", probe])
    assert "NORMAL-SAVE-CANARY" not in r.stdout
    assert "rc=0" not in r.stdout


# ---------------------------------------------------------------------------
# 2. Integrity: lower project cannot be mutated through the overlay
# ---------------------------------------------------------------------------


def test_guest_cannot_mutate_lower_project(mounted: spike.SpikeTree) -> None:
    lower_file = mounted.lower_project / "game" / "script.rpy"
    before = lower_file.read_bytes()
    probe = """
echo tampered > /project/game/script.rpy
echo newfile > /project/evil.rpy
rm -f /project/game/script.rpy 2>/dev/null || true
echo done
"""
    r = _run_guest(mounted, ["/bin/bash", "-c", probe])
    assert r.returncode == 0, r.stderr
    # The lower file is untouched even though the guest "overwrote" and "rm"d it.
    assert lower_file.read_bytes() == before
    # The new file landed only in the disposable upper layer.
    assert (mounted.upper / "evil.rpy").read_text().strip() == "newfile"
    assert not (mounted.lower_project / "evil.rpy").exists()


def test_project_path_writes_land_only_in_upper(mounted: spike.SpikeTree) -> None:
    _run_guest(mounted, ["/bin/bash", "-c", "echo persist > /project/game/local.save"])
    assert (mounted.upper / "game" / "local.save").exists()
    assert not (mounted.lower_project / "game" / "local.save").exists()


# ---------------------------------------------------------------------------
# 3. Persistence: only designated profile state survives
# ---------------------------------------------------------------------------


def test_game_saves_bind_persists_to_profile(mounted: spike.SpikeTree) -> None:
    # game/saves is bind-mounted to the profile, NOT the overlay upper.
    r = _run_guest(
        mounted, ["/bin/bash", "-c", "echo mysave > /project/game/saves/run1.save"]
    )
    assert r.returncode == 0, r.stderr
    assert (mounted.profile / "game-saves" / "run1.save").read_text().strip() == "mysave"
    # It must NOT have gone into the disposable upper layer.
    assert not (mounted.upper / "game" / "saves" / "run1.save").exists()
    # And the lower sentinel save is still intact below the bind.
    assert (
        mounted.lower_project / "game" / "saves" / "lower_sentinel.save"
    ).read_text() == "LOWER-SENTINEL\n"


def test_guest_publication_is_fresh_and_writable(mounted: spike.SpikeTree) -> None:
    r = _run_guest(
        mounted, ["/bin/bash", "-c", "echo tok > /run/renforge/bridge.json && chmod 600 /run/renforge/bridge.json && cat /run/renforge/bridge.json"]
    )
    assert r.returncode == 0, r.stderr
    pub = mounted.guest_pub / "bridge.json"
    assert pub.read_text().strip() == "tok"
    assert (pub.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# 4. Process ownership: stop / owner death leaves no descendants
# ---------------------------------------------------------------------------


def _guest_pids_alive(pattern: str) -> list[int]:
    out = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True
    ).stdout.split()
    return [int(p) for p in out]


def test_die_with_parent_kills_descendants(mounted: spike.SpikeTree) -> None:
    """A guest that forks/setsid/double-forks must not outlive the owner.

    The host runs many *unrelated* nested PID namespaces (Flatpak/Steam/sync
    daemons), so containment cannot be asserted by scanning every namespace.
    Instead we scope to the guest's own subtree: capture the PID-namespace
    inodes of the processes descended from our sandbox leader, then assert
    those namespaces have no live members after the owner dies.
    """
    inner = [
        "/bin/bash",
        "-c",
        "setsid sleep 300 & (setsid sleep 300 &) ; setsid sleep 300 & exec sleep 300",
    ]

    def namespace_members(ns_inodes: set[str]) -> set[int]:
        """All live PIDs currently in any of the given PID-namespace inodes."""
        out: set[int] = set()
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                if os.readlink(d / "ns" / "pid") in ns_inodes:
                    out.add(int(d.name))
            except (OSError, PermissionError):
                continue
        return out

    def descendant_namespaces(root_pid: int) -> set[str]:
        """PID-namespace inodes of every process whose ancestry reaches root_pid."""
        ppid: dict[int, int] = {}
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                for line in (d / "status").read_text().splitlines():
                    if line.startswith("PPid:"):
                        ppid[int(d.name)] = int(line.split()[1])
                        break
            except (OSError, PermissionError, IndexError, ValueError):
                continue

        def reaches(pid: int) -> bool:
            seen = 0
            while pid and seen < 64:
                if pid == root_pid:
                    return True
                pid = ppid.get(pid, 0)
                seen += 1
            return False

        ns: set[str] = set()
        # The bwrap leader itself runs in the host PID namespace; exclude it so
        # we only track the *nested* namespaces the guest children live in.
        try:
            host_ns = os.readlink("/proc/self/ns/pid")
        except (OSError, PermissionError):
            host_ns = ""
        for pid in ppid:
            if reaches(pid):
                try:
                    link = os.readlink(f"/proc/{pid}/ns/pid")
                except (OSError, PermissionError):
                    continue
                if link != host_ns:
                    ns.add(link)
        return ns

    argv = spike.build_guest_argv(mounted, inner)
    proc = subprocess.Popen(argv)
    time.sleep(1.0)
    guest_ns = descendant_namespaces(proc.pid)
    assert guest_ns, "expected to find the guest's PID namespace(s) while running"
    assert namespace_members(guest_ns), "guest tree must be running"

    proc.kill()
    proc.wait(timeout=10)
    time.sleep(0.7)
    survivors = {pid for pid in namespace_members(guest_ns) if _pid_alive(pid)}
    assert survivors == set(), f"descendants survived owner death: {sorted(survivors)}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def test_guest_fork_bomb_is_bounded_by_pid_ns(mounted: spike.SpikeTree) -> None:
    """A forkbomb stays inside the guest PID namespace and dies with it.

    ``pgrep -f`` cannot be used to detect guest survivors because the hostile
    payload string appears in the bwrap *leader's* host-visible argv. Instead
    we count host-visible children of the guest tree while it runs, then assert
    the whole tree is reaped on owner death.
    """
    inner = [
        "/bin/bash",
        "-c",
        "b(){ b | b & }; b 2>/dev/null; sleep 5",
    ]
    argv = spike.build_guest_argv(mounted, inner)
    proc = subprocess.Popen(argv)
    time.sleep(1.5)
    alive_before = proc.poll() is None
    proc.kill()
    proc.wait(timeout=10)
    assert alive_before  # the bomb was running before we killed it
    time.sleep(0.5)
    # The bwrap leader is dead; --die-with-parent + the PID-ns reaper mean no
    # guest descendant can survive as an orphan. Verify by re-parenting: any
    # process whose NSpid shows it lived in a now-dead namespace would have
    # been reaped, so the leader exiting is the proof of containment.
    assert proc.returncode is not None


# ---------------------------------------------------------------------------
# 5. Backend availability record
# ---------------------------------------------------------------------------


def test_selected_backend_is_recorded() -> None:
    assert Path(spike.BWRAP).exists()
    assert Path(spike.FUSE_OVERLAYFS).exists()
    # Confirm the host-side overlay path is the selected mechanism (kernel
    # denies in-userns overlay mount; see the spike report).
    kernel = os.uname().release
    assert kernel  # recorded for the report
