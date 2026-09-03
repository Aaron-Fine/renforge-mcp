# Play Isolation Spike — Result (Task 0, Gate A)

**Status: isolation backend PROVEN. Display-backend render matrix: pending (next step).**

- Date: 2026-08-31
- Host: Fedora 44, kernel 7.1.10-200.fc44.x86_64, bubblewrap 0.11.0, fuse-overlayfs (3.18.2 fusermount3)
- Artifacts: `scripts/spike_play_isolation.py`, `tests/test_play_isolation_spike.py` (9 tests, all passing)

## Selected backend: Architecture B — host-side fuse-overlayfs + allowlisted Bubblewrap

The plan's preferred backend was a Bubblewrap copy-on-write overlay. Two mechanisms
were probed and rejected with recorded evidence before Architecture B was proven:

| Probe | Result | Reason |
| --- | --- | --- |
| `mount -t overlay` by userns-root inside bwrap | REJECTED | Kernel denies overlay mount in a user namespace ("must be superuser"). `CONFIG_OVERLAY_FS=m` exists, but Fedora 44 gates overlay mounts to initial-namespace privilege. |
| `fuse-overlayfs` mounted *inside* the sandbox | REJECTED | `fusermount3` is setuid-root; bwrap's `no_new_privs` blocks setuid elevation, so the FUSE mount cannot be established from inside. |
| **Host-side `fuse-overlayfs` + bwrap bind (Architecture B)** | **PROVEN** | The host caller (unprivileged, owns all dirs) mounts the CoW overlay; the merged view is bound into an empty allowlisted namespace. |

## Proven properties (automated, all green)

1. **Confidentiality** — a hostile guest cannot enumerate or read:
   - the normal-save canary tree (host paths simply absent in the empty namespace),
   - the trusted RenForge state canary (`launch.json` with token),
   - the real home directory (guest `HOME` is a session-private `/home/guest`).
2. **Integrity** — the guest cannot mutate the lower project:
   - overwriting/deleting a lower file through `/project` is absorbed by the overlay,
   - the lower bytes stay identical; new files land only in the disposable upper layer.
3. **Persistence channeling** — `game/saves` is bound to the profile `game-saves/`
   directory (not the overlay); `--savedir`-equivalent state persists only there.
4. **Guest publication** — the single fresh `guest-publication/` dir is writable and
   guest-set `chmod 600` is honored; host cleanup of guest-written files works
   (bwrap's default uid map preserves the caller's uid; no uid-mapping trap).
5. **Process ownership** — with `--unshare-pid` + `--die-with-parent`:
   - `setsid`/double-fork descendants are all reaped when the leader dies
     (verified by scoped PID-namespace-inode tracking, not argv strings),
   - a fork bomb stays inside the guest PID namespace and dies with it.

## Why Architecture B still satisfies the plan

- CoW semantics are enforced by the host-side FUSE mount, *below* the sandbox
  boundary; the guest sees a plain writable directory and cannot bypass it.
- The mount is torn down per session (host-side `fusermount3 -u`), so no
  session state can leak into the next session through the overlay.
- The lower project is structurally un-mutable through the merged view; writes
  to project paths are provably captured in the session `upper/` layer.

## Test-visibility notes for later tasks

- `tests/test_cloud_env_scripts.py` was fixed to assert the demo path is
  repo-relative (`is_relative_to(_ROOT)`) instead of `"​/tmp/" not in path`,
  because all task worktrees live under `/tmp` by design.
- Hostile-probe detection cannot use `pgrep -f <payload>` (the payload string
  appears in the bwrap leader's host-visible argv) nor naive NSpid level
  counting (unrelated desktop Flatpak apps also live in nested namespaces).
  The proven approach scopes to the sandbox leader's descendant namespaces,
  excluding the host's own.

## Display-backend render matrix (pending next step)

Feasibility probed OK: `cage -d` with `WLR_BACKENDS=headless` initializes EGL on
this host; ImageMagick `import` is available for X11 frame capture; the
Task 12 target game (`MyPigPrincess-0.3.0-pc`, Ren'Py 8.2.0) has a bundled
`lib/py3-linux-x86_64` runtime. Remaining: launch the game's bundled `.sh`
under (a) cage headless (`SDL_VIDEODRIVER=wayland`), (b) weston headless,
(c) Xvfb, capture a native frame from each, and record per-backend
socket/ownership/teardown results. The winner becomes the strict-session
default (plan amendment 2026-08-31); Xvfb remains the unconditional CI backend.
