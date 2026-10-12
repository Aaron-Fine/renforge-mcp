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

## First week of Academy34 (2026-10-11, second trial)

A new game, with both skip offers declined, reached the game's end-of-week
message ("You've made it! ... call yourself a cadet") through public MCP tools
only, with no runtime errors. It covered about a dozen free-roam stages: the
parking lot, dorms, Alex's room in several states, the main hall, lecture
theatre, yard, campus by night and day, stadium, fountain, pool, and five
weekday classes.

- Object measuring was not needed. Custom displayables that hold a Ren'Py
  `Action` became controls; other custom regions were clicked at their centre,
  or at a pixel chosen from the screenshot when the centre was transparent.
- `explain(search=...)` traced the bed's vague "explore a little" refusal to
  a chain of five story conditions, without evaluating anything.
- Fixes from the run: engine interface actions count as chrome (dialogue no
  longer stops on every line); `action=None` is disabled; one-pixel key
  listeners and empty canvases don't count as unknown input; actions that
  open a nested interaction reply first; menu captions are substituted;
  reaching a custom hub is `ok`; `explain` searches statements and screens.
- Demo pin: `game/script_version.txt` turns Ren'Py's developer mode off and
  crashed the demo, so the demo pins 8.5.3 in a RenForge-owned
  `renforge.json`. The 8.0.3 live suite went from 13/35 to 36/36 passing.
- Still open: task-app readout missed its list lines; occasional
  `stop=none, steps=0` on a transition frame; a drawn full-screen
  `NullAction()` overlay still counts as unknown input.

## More games (2026-10-11, third trial)

- Divine Intervention (8.4.1): new game to its engine-confirmed ending in about
  eight progression calls, no errors.
- The Darkness Within (8.5.3): played through chapter 1's choices; a bad-ending
  retry loop was my autoplay script's fault, not the tools'.
- Total Degeneracy Island (8.1.2): cabins exterior, four cabin interiors, the
  map, meeting all 22 campers, advancing time, and a 31-piece drag-and-drop
  minigame solved with `act(drop=...)`.
- A House in the Rift (7.6.1): refused cleanly with UNSUPPORTED_RENPY_VERSION,
  by policy. Only RenForge's own activity log was written, then removed.
- Stranded in Space (8.1.1): first cold launch exceeded the 90 s startup
  window; a warm launch was ready in 9 s. Played through day 1's dialogue.

Fixes from this round: screens in the HIDE/OLD phase take no input (a
lingering choice screen froze progression); loads and restarts on 8.0/8.1
reach the engine (they were swallowed without `invoke_in_main_thread`); an
action list containing FileTakeScreenshot opens a game menu and is chrome;
skipping runs about 2.5x faster (5 ms periodic timer during an operation).

Still open: the first launch of a large game can exceed the 90 s startup
timeout; `act` sometimes returns a settled observation from before a location
change (a fresh `observe` is correct).

## Follow-up (2026-10-12)

- Stale `act` result: TDI's `change_location` is `show black; pause 0.5;
  jump`. During the pause the old hub's buttons are still focusable, so the
  pause looked like a settled hub. A timed pause of 5 s or less now reports
  `stable=false, unstable_reason="pause"`; four location changes in a row
  matched a fresh observe, with and without `advance`.
- Startup timeouts: the "cold start" was most likely a locked screen. Ren'Py's
  SDL opens an X11 window and waits for it to be mapped, which never happens
  while the session is locked. Native launches in a Wayland session now use
  `SDL_VIDEODRIVER=wayland,x11`; 8.1–8.5 start in seconds while locked, 8.0
  has no Wayland driver and still needs an unlocked screen or Xvfb. The
  deadline defaults to 180 s, `RENFORGE_STARTUP_TIMEOUT` sets it server-wide,
  and `renforge_launch(timeout=...)` still overrides it per call.

## Later, only if needed

- Tighter bounds for custom hotspots (pixel opacity): not needed so far.
- Fewer agent-facing tools; Live Editor off by default for agent sessions.
- Split `bridge.rpy` into a few files.
- `source` for menu choices drawn in a transformed viewport.

## Risks

- Widgets built in Python (custom displayables, `renpy.show`) have no
  `_location`; report `source=null`.
- One `_location` can appear many times on screen (`use`, loops); treat it as
  the statement, not the instance.

## Follow-up (2026-10-12, later)

- Flaky village-gate test: at startup, the `scene ... with dissolve`
  interaction can outlive the dissolve's delay, showing only the quick menu,
  and progression stopped with `stop=none`. Short timed pauses now stay
  unsettled for their delay plus one second; 30/30 repro runs pass.
- RenForge state lives outside the game (`RENFORGE_STATE_DIR`, default
  `~/.local/state/renforge/projects/<name>-<hash>/`).
- Tool count: upstream has 55 tools; this fork added only observe, act,
  advance_until, explain, and mod, so nothing qualified for removal.
- observe/act/advance_until return compact observations by default
  (`detail=full` for everything); act accepts `observation_id` alone.
- Isolated sessions start with transitions off (`RENFORGE_TRANSITIONS=on`).
- list_ui_elements and related upstream tools have upstream ids again; an
  earlier branch commit had broken the live demo acceptance tests.
- Upstream 0.7.3 merged. Skipping is about 7x faster (vsync off for
  launched games, `config.skip_delay` 0 during an operation, one check per
  line, status polling, plain containers in the geometry walk): 19 ms a line
  on the demo GUI, ~15 ms in Stranded in Space. Observations and crossed
  lines report `seen` for statements already finished since launch.
- Fork-only `RENFORGE_TOOLS` / `RENFORGE_TOOLS_EXCLUDE` limit the listed
  tools during development (remove before upstreaming).
- Task-app readout fixed (viewport content is readout). The fountain's
  "NullAction overlay" was Academy34's Interactable, whose `click_action`
  also fires the game's event hooks; displayables with one `*click*` action
  attribute are now controls, so free-roam hotspots are acted on by id and
  hubs stop as `choose`, not `unsupported`. Clipped menu choices have
  `source`.
- Still open: the CI artifact change waits on local branch
  `ci/state-folder-artifacts` (needs a token with `workflow` scope).

## Play-toolset trial (2026-10-12, evening)

Server limited to `RENFORGE_TOOLSETS=play` (info, session, observe, act,
explain, mod). Games: Scions of the Divine (8.4.1), Fleeting Memories (8.3.2),
Dawnfall (8.5.0), plus re-checks of Total Degeneracy Island and Academy34.
Little Green Hill chapter 2 cannot start on its own (it continues a chapter 1
save), and Fleeting Memories needed eight decompiled loose `.rpy` files removed
from the copy (they duplicated the archive and stopped Ren'Py at a parse error).

Fixed from the trial: progression skips through pauses and transitions
(lines with `with diss` ran at ~170 ms each); a time-budget stop is ok;
continuing is never refused as stale; session saves persist in the project
state folder; HUD screens kept up from earlier lines do not stop a clickable
line; continuing clicks past a line held only by optional buttons; an advance
that repeats a line stops with `stop="loop"`.

Gaps noted: the play loop cannot start at a label (only `renforge_jump` in the
`live` toolset); text drawn into images (Fleeting Memories' intro) has no
readout; a copied save from another folder triggers Ren'Py's "trust this
device" prompts, which the agent answers like a player.

Second play-toolset trial: Business of Loving (8.5.0) through the intro,
naming, and free roam of the house (rooms, talking to Lorraine, chores, bed);
MurMur (8.3.7) through a custom naming screen, the prologue, a hard-pause POV
button, and into chapter 1. Fixes: act tolerates unrelated screen changes when
its target control is unchanged (a closing notification made the hub stale);
menu captions drop text tags; readout follows ImageReference targets
(`show text`); inputs report `value`; SensitiveIf/SelectedIf do not block
chrome; continuing that cannot progress says why.
