# Fork sync with upstream 0.7.6 and polish pass

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Bring fork `main` up to upstream `main` (3ba2858, release 0.7.6, includes the merged #125), then polish the fork-only code that the next upstream PR (agent play loop) will carry, so that PR draws few review findings.

**Architecture:** One merge commit on branch `sync/upstream-0.7.6` (worktree `~/renforge-mcp/.worktrees/sync-upstream`), then a read-only review panel split by area, then fix commits, then a fork PR (CodeRabbit + CI) and a dry-run of the next upstream PR.

**Tech stack:** Python 3.11+, Ren'Py bridge (`bridge.rpy`), pytest, GitHub Actions (Ubuntu, macOS, Windows), CodeRabbit (installed on the fork; config PR #27).

**Spec:** none beyond this plan and the user's request: "Sync, then next-PR code" polish. Rulings are provisional.

## Global Constraints

- Test command (from the worktree): `PYTHONPATH=src /home/aaron/renforge-mcp/.venv/bin/python -m pytest -q -p no:cacheprovider`. `ui/node_modules` is a symlink to the main checkout's (needed by i18n tests).
- Code, tests, docs and changelog name no specific games.
- Conventional commits, each ending with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_011nJrsRbYiyTDVvyy9QoVnS`.
- Implementers never push, never post to GitHub, never start reviewer subagents (Haiku read-only helpers per the user's CLAUDE.md are fine).
- Windows code paths run only on GitHub CI (windows-latest).
- Fork-only files (never go upstream): `TODO.md`, `docs/superpowers/**`, `scripts/capture_observe_baseline.py`, `.claude/agents/**`, `.coderabbit.yaml`.

## Review Focus

- Isolation regressions from the merge: a launch, relaunch (jump/new_game/Warp) or session reuse that exposes the player's saves or HOME when upstream's code would not.
- Fork-only security gates (authorize for reading the player's saves / launching into foreign folders, eval toolset gating) silently dropped by taking upstream's side of a conflict.
- Public tool contract drift: `tool_definitions.py`, `docs/MCP.md` and `tests/snapshots/mcp_public_tool_contract.json` disagreeing.
- Windows-only code in `util/files.py` diverging from upstream's CI-proven version.
- Upstream features (#116 write receipts, #117 scene_tree verdicts, #118 shadow lint, #121 RENPY_LANGUAGE) lost or half-merged.

---

### Task 1: Merge upstream main

- [ ] Run the suite on `origin/main` (3272045) first and record pre-existing failures.
- [ ] `git merge upstream/main` (3ba2858). Expect ~200 conflict hunks in 38 files, mostly `save_isolation.py`, `tools/live.py`, `tool_definitions.py`, `tool_registration/lifecycle.py`, `bridge/launcher.py`, `ui/server.py` and their tests.
- [ ] Resolve by this rule. Upstream #125 is the reviewed, hardened version of the shared isolation code (no-follow/TOCTOU copy, Windows anchoring, per-component reuse and relaunch inheritance, opt-in `savedir=temporary`). For that code, upstream's side is the base. Fork-only features that #125 deliberately left out stay, re-applied on top of upstream's code: the authorize gate for the player's saves and foreign folders, the eval/mod toolsets, `renforge_session`, the play loop, screen key bindings. Where a fork change is an older version of something #125 improved, take upstream's. When unsure, keep the stricter behaviour and list it in the report.
- [ ] CHANGELOG: upstream's released sections verbatim; fork Unreleased entries stay, minus anything now in a released upstream section. Versions follow upstream (0.7.6).
- [ ] Regenerate the contract snapshot with the repo's own tool (find it in tests/ or scripts/); every snapshot change must be explained by a merged change.
- [ ] Full suite passes apart from the recorded pre-existing failures. Commit the merge (merge commit, not rebase).

### Task 2: Review panel (read-only)

Scope: `git diff upstream/main sync/upstream-0.7.6 -- src tests` minus fork-only files. Four reviewers, each writing a findings file: (A) bridge: `bridge.rpy`, `bridge/guard.py`, `bridge/client.py`, `bridge/artifacts.py`, `bridge/launcher.py`; (B) play loop: `interaction.py`, `progression.py`, `compact.py`, `toolsets.py`, `mods.py`, `tools/mods.py`, `tool_registration/interaction.py`; (C) server side: `tools/live.py`, `tool_registration/lifecycle.py`, `ui/server.py`, `activity_log.py`, `policy.py`, `editor/*`, `sdk.py`, `tool_definitions.py`; (D) simplification/consistency across the whole scope. A, B and C each apply the CodeRabbit checklist: one boolean standing for several independent settings; actions documented as read-only that write; Windows paths weaker than POSIX; missing docstrings on new or changed public functions; per-entry stats or whole-file reads where streaming or names suffice.

### Task 3: Fix waves

Controller triages the findings (ledger rulings). One implementer per area, sequentially, each fix with a regression test that fails before it where the platform allows.

### Task 4: Fork PR and dry run

Panel-reviewed fixes, then push `sync/upstream-0.7.6`, open a fork PR into fork `main` (CodeRabbit + CI incl. Windows), fix findings. Merging into fork `main` waits for the user.
