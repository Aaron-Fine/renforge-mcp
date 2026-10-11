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

## Later, only if needed

- Tighter bounds for custom free-roam hotspots (sample pixel opacity).
- Fewer agent-facing tools; Live Editor off by default for agent sessions.
- Split `bridge.rpy` into a few files.
- Fix repeated labels like `RETURN: RETURN: RETURN`.

## Risks

- Widgets built in Python (custom displayables, `renpy.show`) have no
  `_location`; report `source=null`.
- One `_location` can appear many times on screen (`use`, loops); treat it as
  the statement, not the instance.
