# Minimal Play Implementation — Status

**Plan:** `2026-08-31-renforge-end-to-end-minimal-play.md` (MVP 1: Safe Eyes and Hands)
**Last updated:** 2026-08-31, session 1 close-out

## Where things stand

| Task | State | Branch / artifact |
| --- | --- | --- |
| Plan review + amendments | Done | `feature/end-to-end-minimal-play-plan` |
| Task 0 — isolation half | **Proven** (9/9 hostile tests) | `play/task0-spike` @ `35ce6fe` |
| Task 0 — display render matrix | Pending (feasibility probed OK) | `play/task0-spike` |
| Tasks 1–3 (wave 1) | Not started; worktrees ready | `play/task1-contracts`, `play/task2-preflight`, `play/task3-profiles` |
| Tasks 4–12 | Not started | worktrees to be created per wave |

## Ratified decisions (do not re-litigate)

1. **Display backend:** pluggable; cage headless (native Wayland) → weston headless → Xvfb; Xvfb unconditional in CI. Amended into the plan (amendment note, §5, Tasks 0/4/5/11, verification, risks).
2. **Task 0 owner:** main session (done for isolation half).
3. **Orchestration:** subagents (glm-5.3-flash) commit on their own task branches; main session reviews, gates, merges. Shared seams (`bridge.rpy`, `tools/live.py`, tool snapshot) serialized per §9.
4. **Task 12 target:** `/home/aaron/Spicey/hgame/MyPigPrincess-0.3.0-pc` — Ren'Py **8.2.0.24012702**, complete loose `.rpy` tree (29 files), bundled launcher. Its `renpy.input()` name prompt (`game/Script/Chapter1/1-Chapter 1 Intro.rpy:152`) is out of MVP 1 scope → handled by user-supplied `walkthrough_mod.rpy` defaulting the name.
5. **Interpreter:** uv-managed Python 3.12 everywhere (installed; host default 3.14 works but CI matrix is 3.11/3.12).
6. **Worktrees:** `/tmp/renforge-play-wt/<task>-<name>` on branches `play/task<N>-<name>`.

## Isolation backend (Task 0 result)

**Architecture B: host-side `fuse-overlayfs` CoW mount + empty allowlisted bwrap namespace.**

- In-userns `mount -t overlay`: rejected by Fedora 44 kernel (denied to userns-root).
- FUSE mounted inside sandbox: rejected (bwrap `no_new_privs` blocks setuid `fusermount3`).
- Host-side FUSE + bind: **proven** — guest writes land in session `upper/`, lower project byte-pristine, `game/saves` binds to profile, guest publication fresh + `chmod 600` honored, uid mapping clean (host-owned guest files, cleanable).
- Fail-stop proven: `--die-with-parent` + PID namespace reap all `setsid`/double-fork/fork-bomb descendants. Detection lesson: scope to the leader's descendant namespace inodes — `pgrep -f` and NSpid counting both give false positives (leader argv / desktop Flatpak apps).

Full detail: `docs/superpowers/reports/play-isolation-spike.md`.

## Remaining Task 0 items (Gate A closure)

1. Display render matrix: launch MyPigPrincess's bundled `.sh` under cage → weston → Xvfb, capture a native frame from each (`import` for X11; cage EGL confirmed working), record socket/ownership/teardown per backend.
2. Negative exercises: unsafe symlinks, FIFOs, bind targets, missing overlay support, insufficient copy space, diagnostic preservation.
3. `--savedir`/MultiPersistent/XDG byte-landing proofs (fold into real-engine launch work in Tasks 5–6 if display matrix lands first).

## Environment facts (Fedora 44 host "wells")

- bwrap 0.11.0, fuse-overlayfs + fusermount3 3.18.2, cage 0.3.1, weston 15.0.1, gamescope, Xwayland, Xvfb + xvfb-run all installed.
- Python: host 3.14.7; uv-managed 3.12.12 installed. Existing suite: 914 passed / 11 failed on base commit — 10 i18n-scanner failures + 1 cloud-env failure are **pre-existing on main** (known-bad baseline; exclude from regression gates, fix out of scope). The cloud-env test was fixed for `/tmp` worktrees (`is_relative_to(_ROOT)`).
- No Ren'Py SDK cached locally (`RENPY_SDK_HOME` empty); SDK download mechanism exists (`sdk.py`, default 8.5.3) and MyPigPrincess supplies its own bundled 8.2.0 runtime.
- Host runs many nested-namespace desktop apps (Steam, Flatpak) — irrelevant to isolation but relevant to test detection strategy.

## Next session

1. Finish Task 0 display matrix + negative exercises → close Gate A, update report + checkboxes.
2. Kick off wave 1 (Tasks 1–3) with glm-flash subagents in the existing worktrees; review + merge into `feature/end-to-end-minimal-play-plan` in order 1→2→3.
