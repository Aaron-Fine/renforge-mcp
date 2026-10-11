# Engine-native perception and an agent modding loop

Date: 2026-10-11. Engines checked: Ren'Py 8.0.3 and 8.5.3 sources.

Goal: an agent can understand any Ren'Py 8.x game without game-specific code,
then write, test, and remove mods.

## Three steps

### 1. Read controls from the engine instead of recomputing them

The bridge's control collector (`_renforge_observe_focus_pairs`) rebuilds
geometry, modal blocking, and mask hits by hand. The engine already has them:

- `renpy.display.focus.focus_list`: every focusable widget from the last drawn
  frame, with its screen-space rect (transformed, clipped) and owning screen.
  Widgets behind a full-modal screen are already removed.
- `renpy.display.render.screen_render.focus_at_point(x, y, None)`: the engine's
  own click hit test (layers, modals, `focus_mask`). Use it to find a click point.
- Menu choices: keep reading `items` from live screens (works today).
- Disabled buttons aren't in `focus_list`; find them in live screens and report
  `enabled=false`.

Delete the modal-ranking, mask-emulation, and no-render fallback helpers. Keep
the render-tree walk only for `scene_tree`, text bounds, and custom hotspots.
Output format stays the same.

Done when: observe output on the live fixtures matches the old output (record
it first), and the test suites pass on 8.0.3 and 8.5.3.

### 2. Show where things come from: one `renforge_explain` tool

Every widget built from screen language has `_location` (file, line). Add
`source: "file:line"` to each control. Then add one read-only tool:

- `explain(id)`: the control's `action`/`sensitive` source text and the `if`
  conditions around it, read from the screen's syntax tree (nothing evaluated).
- `explain(label)`: the label's statements in order: dialogue, menu captions
  with their conditions, `if` branches, jumps/calls, with file:line.
- `explain(screen)` / `explain()`: defined screens (`renpy.display.screen.screens`)
  and labels (`renpy.get_all_labels()`) with locations.

This runs inside the game, so it works for games that ship only `.rpa`/`.rpyc`
(e.g. Academy34 has no `.rpy` files).

Done when: on a fixture packed as `.rpyc` only, explain reports a gated
hotspot's condition and a menu's conditional items.

### 3. Mod loop: one `renforge_mod` tool

- `write`: files go only under `game/mods/<name>/`, recorded in a manifest.
- `reload`: lint the mod files, `reload_script`, return errors + a fresh observation.
- `remove`: delete exactly the manifest's files and reload.
- A short modding-guide resource: `config.label_overrides`, overriding a screen
  with a later `init offset`, `config.overlay_screens`, `default` vs `define`.

Done when: an agent adds a button to a hub screen, clicks it via observe/act,
removes the mod, and the original screen returns.

Commit after each step.

## Status after implementation and an Academy34 trial (2026-10-11)

All three steps are implemented on `feat/engine-native-perception`. The trial
drove a copy of Academy34 (archive-only: no `.rpy` files) through the public
MCP tools only: main menu → startup choices → name input → free-roam room.

What worked:

- Every ordinary control and every custom input region carries `source`
  (`file:line`), even with no source files on disk.
- Two `explain` calls showed how the game's free roam works: hotspot `X` at
  location `L` calls label `location_L_interact_X`. `explain(screen=...)`
  listed the room's 30 interactables and the `event_available(...)` gates;
  `explain(source=...)` showed that the bed region triggers `interact_bed`.
- The mod loop worked end to end: write an overlay button → reload (game state
  kept) → observe shows it with its mod source → act runs it → package →
  remove (deleting `main.rpy` and `main.rpyc`) → reload → button gone.

Re-evaluation:

- Step 1 fixed real bugs (disabled buttons reported as usable; "selected" and
  "bar" leaking into labels) but removed far less code than planned. The render
  walk is still needed for text bounds and the not-yet-drawn check, so it can't
  be made lazy.
- The trial's first full index was 200 KB; it now takes `query`, honours
  `limit`, and reports file counts. Explain also takes `source`.
- The biggest remaining gap is free-roam geometry: most Academy34 props render
  a full-frame canvas, so their bounds cover the whole screen. `explain` now
  tells the agent *what* each prop is; it still can't tell *where* to click.
  Sampling pixel opacity is now clearly the next most valuable change.
- Menu choices drawn in a transformed viewport have no `source`.
- On Ren'Py 8.0.3, 22 of 35 live tests fail both before and after these changes;
  the demo game needs a newer engine. That's test debt, not a regression.
- Stopping a session deletes its temporary saves; pass an explicit `savedir`
  to keep checkpoints across launches.
- Haiku implemented both new tools from task cards. Opus still had to fix tool
  counts after merging and two security issues (mod writes must be
  authorized like `eval`; writes must not follow symlinks). Agent worktrees
  started from a stale commit; task cards must check their base commit.

## Later, only if needed

- **Next:** tighter bounds for custom free-roam hotspots (sample pixel opacity).
- Fewer agent-facing tools; Live Editor off by default for agent sessions.
- Split `bridge.rpy` into a few files.
- `source` for menu choices drawn in a transformed viewport.

## Risks

- Widgets built in Python (custom displayables, `renpy.show`) have no
  `_location`; report `source=null`.
- One `_location` can appear many times on screen (`use`, loops); treat it as
  the statement, not the instance.
