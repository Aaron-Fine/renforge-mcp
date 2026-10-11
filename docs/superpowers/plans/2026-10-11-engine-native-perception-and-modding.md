# Engine-native perception and an agent modding loop

Date: 2026-10-11. Branch at time of writing: `feat/advance-until-crossed`
(`9c2a949`). Engines checked: cached Ren'Py SDKs 8.0.3 and 8.5.3.

## Goal

An agent should be able to (1) see and understand any Ren'Py 8.x game,
including free-roam hubs, without game-specific code, and (2) use that
understanding to write, load, verify, and remove mods. Prefer reading what the
engine already computed over recomputing it.

## Where the code is today

Observation, listing, hit-testing, and acting share one collector,
`_renforge_observe_focus_pairs` (`src/renforge/bridge/bridge.rpy:4004`). Per call it:

1. Walks every layer's displayable tree (`_renforge_scene_inventory`, up to 5,000
   nodes), recording owning screen and widget IDs.
2. Re-walks the last completed `Render` tree and composes affine matrices and
   clip polygons by hand (`_renforge_render_geometry`, about 125 lines).
3. Reads `renpy.display.focus.focus_list`, then overrides the focus rectangles
   with its own geometry for widgets it has also found in the render walk.
4. Adds inputs, bars/viewports/drags, every visible `Button` in the inventory
   that the focus list did not contain, and menu `items` entries.
5. Decides coverage with screen-flag modal ranking, a no-render containment
   fallback, mask emulation, and nine `focus_at_point` probes per control
   (`_renforge_mark_coverage` and helpers, about 180 lines).
6. Reports non-engine `event` overrides as `input_candidates`.

All of it is game-agnostic, and it works on the live fixtures and the Divine
Intervention and Academy34 runs. Much of it, though, duplicates computations
the engine has already done for the same frame.

## What the engine already provides

Checked in the 8.0.3 and 8.5.3 sources:

| Engine fact | Where | What it replaces |
| --- | --- | --- |
| `focus.focus_list`: `Focus(widget, arg, x, y, w, h, screen)` for the last drawn frame | `display/focus.py` `take_focuses()` ← `Render.take_focuses` | Hand-composed geometry for every focusable control. Rects are already in screen space, transformed, and clipped to viewports. Fully clipped focuses are dropped. |
| A full-modal render clears every focus below it, and `modal="window"` drops focuses inside its rect | `render.pyx` `take_focuses` | Screen-flag modal ranking (`_renforge_modal_rank`, `_top_modal`, `_modal_blocks`, `_full_modal`, `_same_screen`). |
| `screen_render.focus_at_point(x, y, None)` is the engine's own mouse hit test: layer order, callable modals, `focus_mask` pixels, `Modal` sentinel | `render.pyx` | `_renforge_mask_allows` and the no-render containment fallback. One engine probe answers "would a click here reach this widget?" |
| `Focus.screen` is the owning `ScreenDisplayable` | `focus.py` | The `contexts` map and the `SimpleNamespace` re-wrapping in `add()`. |
| `Button.focusable` is false exactly when the button has no `clicked` action, so insensitive buttons are missing from `focus_list` | `behavior.py` `Button.per_interact` | Explains the one legitimate reason to walk screens for buttons: reporting disabled ones. |
| `Displayable._location = (filename, line)` on every SL2-created displayable | `sl2/slast.py` (`main._location = self.location`) | Nothing yet. Not used by the gameplay bridge; the editor already uses it. **This is the key fact for modding.** |
| `renpy.display.screen.screens[(name, variant)]` → `Screen` with `ast` (an `SLScreen`), `location`, `parameters`, `modal`/`zorder`/`tag`/`layer` source strings | `display/screen.py` | Nothing yet. These are definitions, not live instances, so they serve as a catalogue of every screen the game defines, with source locations, even when the game ships only `.rpyc`/`.rpa`. |
| SL AST keeps source text: `SLDisplayable.keyword = [(name, expr_source)]`, `SLIf.entries = [(cond_source, block)]` | `sl2/slast.py` | Nothing yet. A control's `action`, `sensitive`, `hovered`, and the `if` conditions guarding it are all readable as source strings without evaluating anything. |
| `renpy.game.script.namemap` / `renpy.get_all_labels()`, nodes with `filename`/`linenumber`; `renpy.ast.Menu.items` carry condition source; `If.entries`, `Python.code.source` | `script.py`, `ast.py` | Nothing yet. A runtime source index for games without `.rpy` files. |
| `renpy.loader.load(path)` reads files out of `.rpa` archives through the game's own loader | `loader.py` | Nothing yet. Static tools are blind to archived games (Academy34's `game/` has zero `.rpy` files). |

`renpy.display.screen.screens` does not answer "what is clickable now". It
holds definitions. Live instances stay in `scene_lists.layers`, and `Focus.screen`
already points at them. The definitions do answer "where is this screen defined,
and what are its parameters", which modding needs.

## Simplification plan

### S1. Make `focus_list` + `focus_at_point` the control backbone

New collector (same public record shape, so no MCP contract break):

```text
for f in focus_list (in order; later = higher):
    skip f.x is None (global focus: dismiss/keyboard), but note SayBehavior presence
    bounds   = (f.x, f.y, f.w, f.h)                 # engine screen space
    screen   = f.screen                             # ScreenDisplayable or None
    role     = Button / Input / Bar / Viewport / Drag from isinstance on f.widget
    hit      = first of [center, 8 probe points] where focus_at_point() returns (f.widget, f.arg)
    click_point = hit; covered = hit is None and render available
    source   = f.widget._location                   # S2
menu entries: screen.scope["items"] for screens on scene_lists (unchanged logic)
disabled buttons: Buttons in live screens with clicked is None and not in focus_list
                  -> enabled=false, bounds from render_of lookup only if cheap
```

Remove: `_renforge_modal_rank`, `_renforge_top_modal`, `_renforge_modal_blocks`,
`_renforge_full_modal`, `_renforge_same_screen`, `_renforge_mask_allows`, the
no-render branch of `_renforge_mark_coverage`, the `contexts` re-wrapping, and the
"visible Button not in focus list ⇒ enabled control" path. That last path also
removes a source of controls that the engine would never focus.

Keep `_renforge_render_geometry`, but stop calling it from the control path. It
remains for `scene_tree`, readout text bounds, `input_candidates`, and
disabled-button bounds, and is computed lazily only when one of those is
requested. Observe becomes cheaper and simpler to reason about.

Things to verify with the existing live fixtures before deleting code:

- Viewport-clipped menu entries still resolve through `items` (they already do)
  and are reported `semantic=true` with no `click_point`.
- `focus_list` reflects the last draw, so the existing "screen not yet drawn ⇒
  `not_ready`" guard still matters. Replace the full render walk used for it
  with a cheap check: each live screen root's `id` appears in some
  `render_of` at shallow depth. Alternatively, record the draw count in an
  overlay sentinel's `render`.
- Transformed/zoomed choice screens (Academy34 gap 3) get correct rects from
  `take_focuses`, because it applies the full transform. Add a regression
  test comparing old vs new bounds.
- Window-modal and callable-modal screens: `focus_at_point` decides, and we
  stop guessing from flags.

Acceptance: golden observe JSON for the demo game, `say_what_clean`, the
custom-layer-scaled fixture, the render-hover fixture, and the menu/viewport
fixtures is unchanged except for documented field removals. Net deletion of
about 300 lines from `bridge.rpy`. Live suite passes on 8.0.3 and 8.5.3.

### S2. One identity source: `_location`

Control IDs currently fall back through widget ID → menu index → drag name →
action summary → image → ordinal. Add `source: "game/screens.rpy:412"` to every
control, readout, and candidate that has `_location`. Use `(screen, id or
location, occurrence)` as the preferred synthetic identity. It is stable across
rebuilds and meaningful to a modder. Keep the existing IDs as aliases for one
release.

### S3. Narrow the agent-facing tool surface

There are about 60 tools. For play and modding an agent needs about 10:
`launch`, `launch_status`, `observe`, `act`, `advance_until`, `screenshot`,
`click_at`, `saves`, `eval`, `stop`, plus the modding tools below.
`list_choices`, `select_choice`, `list_ui_elements`, `click_element`,
`hover_element`, `get_ui_element_bounds`, `hit_test`, `advance`, `send_input`
are views over the same collector.

- Add a `RENFORGE_TOOLSET=play|modding|editor|all` selection (default `all` for
  upstream compatibility) that registers only the relevant tools. Fewer
  overlapping tools mean fewer wrong picks by agents.
- Default `editor=false` when the toolset is `play` or `modding`. The Live
  Editor (`editor.rpy`, 7.4k lines, about 20 runners) is an upstream human
  feature. Leave it untouched to keep merges cheap; don't build on it.

### S4. Split `bridge.rpy` by concern (mechanical, last)

6.7k lines in one `init python` block. Split into transport/security (Windows
ACLs, ready-file, listener: about 1.3k lines), perception (inventory, collector,
dialogue, readout), actions/progression, and control/saves. The launcher already
injects multiple resources. Do this after S1 so the moved code is the smaller
code. Pure move; no behavior change; one commit.

## Extensions needed for modding

### M1. Runtime source index (works on `.rpa`/`.rpyc`-only games)

New bridge handler + tool `renforge_runtime_index(kind=labels|screens|images|
variables|files, filter, limit)`:

- labels: `namemap` entries that are `renpy.ast.Label`, with file/line, and
  whether the file has `.rpy` source on disk or only in an archive.
- screens: `renpy.display.screen.screens` → name, variant, parameters, layer,
  tag, zorder/modal source text, location, currently shown on which layers.
- images: `renpy.display.image.images` tags/attributes.
- variables: `default`/`define` statements from `script.all_stmts` with
  location (verify it stays populated after load; otherwise walk `namemap`
  values), current value via the existing JSON-safe store snapshot.
- files: `renpy.exports.list_files()` (includes archived files).

Plus `renforge_read_game_file(path)` via `renpy.loader.load`, bounded size,
for archived `.rpy`, text, and images. Static tools (`scan_project`,
`find_references`) should say when a project is archive-only and point to the
runtime index instead of returning silently empty results.

### M2. Explain a control or label from AST source text (no eval)

`renforge_explain(id=… | label=… | screen=…)`:

- control: find the `SLDisplayable` at the widget's `_location` in its
  screen's `ast`. Return keyword source (`action`, `sensitive`, `hovered`,
  `selected`) and the chain of enclosing `SLIf`/`showif` conditions as strings.
  This answers Academy34's open gap ("conditional hotspots look actionable;
  eligibility unknown") generically: the agent reads
  `if quest_stage >= 3 and not door_locked:` instead of guessing.
- label: bounded walk of the label's statement chain: say (who + first chars),
  menu (captions + condition source), `if` branch conditions, jump/call targets,
  python source excerpts, with file/line. This is a game-agnostic decompile-lite
  that covers route understanding without unrpyc.
- screen: the SL tree outline with locations.

All read-only; nothing is evaluated, so it is safe under the existing policy.

### M3. Mod authoring loop

`renforge_mod(action=create|write|list|lint|reload|remove|package, name, files)`:

- Files live under `game/mods/<name>/` (Ren'Py loads `.rpy` recursively),
  tracked in a manifest so `remove` restores the tree exactly. Never edit game
  files in place. Refuse paths outside the mod directory.
- `lint` runs the existing lint and filters to mod files.
  `reload` = `reload_script`, then wait for a stable observation; report new
  runtime errors from `get_errors`.
- `package` zips `game/mods/<name>/` with a generated README listing hooks and
  required engine version.
- Ship a `renforge://modding-guide` MCP resource covering non-invasive hooks:
  `config.label_overrides`, `init offset`/priority to override a screen by
  redefining it later, `config.overlay_screens`, `config.label_callbacks`,
  `config.after_load_callbacks`/`config.start_callbacks`, `default` vs `define`
  and save compatibility, and `renpy.call_in_new_context` for injected UI.
- Verification recipe: launch isolated → load a copied user save (exists) →
  reload with mod → observe → `explain` the new control → act → check errors →
  remove → reload → confirm original behavior.

### M4. Free-roam hotspot precision

Custom `event` displayables (Academy34 parking/room hotspots) never enter
`focus_list`, so they stay `input_candidates`. Their render bounds often cover
the whole frame. Spike first, then implement:

- For each candidate's render occurrence, sample `Render.is_pixel_opaque` on a
  bounded grid (e.g. 48×27) to produce an opaque bounding box and a proven
  interior point. Report `opaque_bounds`, `probe_point`, and cost.
- Combine with M2. A candidate with `_location` gets its SL source and
  conditions. Pure-Python CDDs at least get the class's source file from
  `type(node).__module__` and `inspect.getsourcefile`, when available.
- Keep the existing click fallback (synthetic hover → click) as the actuator.

Acceptance: on Academy34's room, each visible hotspot gets a box no larger than
its sprite, and a `click_at(probe_point)` reproduces the earlier manual clicks.

### M5. Small, obvious gaps

- A `label_callbacks` trace in the event stream (`label_entered` with file/line).
  This lets an agent map free-roam actions to the labels they reach without
  reading code.
- The TODO's deferred text normalization (`RETURN: RETURN: RETURN`,
  `Barbar`): with S1, use `_tts_all` only on the focus widget, not on
  ancestors, and use `bar` + adjustment range as the label for unnamed bars.
- Ending evidence for custom end screens stays out of scope (TODO is right);
  M5's label trace gives agents evidence without the bridge inferring it.

## Order and checkpoints

| Phase | Work | Gate |
| --- | --- | --- |
| 0 | Golden-output harness: capture current observe/list/hit_test JSON on all live fixtures for 8.0.3 and 8.5.3; time `observe`. | Baseline committed. |
| 1 | S1 collector on `focus_list` + `focus_at_point`; lazy render walk. | Goldens equal (diff reviewed), full Python suite, live suite on both engines. |
| 2 | S2 `source` field, M5 text normalization. | Goldens updated intentionally. |
| 3 | M1 runtime index + archive reader. | Fixture packed into an `.rpa` with `.rpyc` only gives the same index as source. |
| 4 | M2 explain. | Conditions/action source for a gated fixture hotspot and a menu with conditional items. |
| 5 | M3 mod loop + modding-guide resource. | End-to-end: create mod adding a button to a hub, verify via observe/act, remove, verify original. |
| 6 | M4 spike → implementation. | Academy34 acceptance above (manual, local game). |
| 7 | S3 toolsets, then S4 bridge split. | Contract snapshot per toolset; pure-move diff. |

Commit after each phase. Phases 1–2 are the simplification. Phases 3–5 make the
modding goal reachable. Phase 6 is the free-roam quality step.

## Risks and open questions

- `focus_list` is rebuilt per draw. Reading it from the bridge thread while the
  engine draws would race. The bridge already services requests on the main
  thread (`renforge_drain_bridge`); keep it there.
- `_location` is absent for displayables built in Python (CDDs, `ui.*` calls,
  `renpy.show`). Report `source=null` explicitly.
- SL `const`/cached nodes (`SLCache`, `use` transclusion) mean one
  `_location` can map to many instances. Treat location as a statement
  identity, not an instance identity (the editor's cache logic already handles
  this and is a reference).
- 8.0–8.5 differences: `_tts_all` signature (already handled), `exports` split
  into a package in 8.2+ (use `renpy.*` names, not module paths).
- Modding archived games may need `init offset` larger than the game's highest
  init priority. The index should report the max observed init priority.
