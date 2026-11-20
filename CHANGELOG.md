# Changelog

All notable RenForge releases are recorded here. Versions follow semantic
versioning.

## Unreleased

### Changed

- Under `RENFORGE_POLICY=enforce`, reading the player's own saves needs
  `authorize=true`: `renforge_saves` `list_user` and `import` and
  `renforge_session` `import` are destructive, and `renforge_session list`
  leaves out `player_slots` (with `player_slots_hidden`) unless authorized.
  A launch that keeps the player's saves or HOME is destructive, and so is
  one whose `savedir` or `home` is a path outside the RenForge state folder
  (the shared temp dir is not RenForge's), since the game writes its saves,
  persistent data, and config there. With isolation opt-in (0.7.6), that
  includes a launch that passes none of the isolation options unless
  `RENFORGE_ISOLATION=temporary`, and a `savedir` path with `home` omitted
  (it keeps the player's HOME); pass `savedir=temporary` to launch without
  `authorize`. A `renforge_jump` or `renforge_new_game` is judged by its own
  arguments, so one with no isolation options needs `authorize` even when it
  would inherit an isolated game's isolation. `renforge_launch`,
  `renforge_jump` and `renforge_new_game` keep their `authorize` parameter
  for this, and a denied launch's `next_step` names `savedir="temporary"`
  first. The policy reads the path the launch uses, stripped and expanded.
- A launch reuses a running game only when it matches the request. On top
  of 0.7.6's per-component check, it is refused with
  `SESSION_ISOLATION_MISMATCH` when the launch keeps its saves or HOME in
  RenForge's own folders and the game keeps that one elsewhere (the
  player's saves or HOME, or a folder an earlier launch was authorized
  for). Saves and HOME are judged apart, so `savedir=temporary` with
  `home=existing` is never handed a game whose saves are elsewhere. It is
  refused with `SESSION_EVAL_MISMATCH` when a play-only server would get a
  bridge that runs code. The policy gate and this check share one rule. A
  launch that arrives while another starts is refused the same way instead
  of being told to poll it, and `renforge_launch_status` reports `isolated`
  and `eval_enabled`. A game this server did not start answers the new
  bridge command `session_info` (its eval switch, savedir, and HOME); one
  that cannot is assumed to match neither, and one that answers without
  `eval_enabled` (an upstream bridge) is assumed to run code. A game this
  server did not start counts as in RenForge's own folders only when its
  HOME and every save location it reports are (the state folder, where
  `renforge_session` keeps its saves, or a session root) and none of them
  resolves into the player's save tree; a `renforge_session` game started by
  another server is reused and reported as isolated. Launches through the dashboard
  pass the calling server's `bridge_eval`, so a play-only server's game
  refuses eval even when the dashboard's own toolsets allow it.
- The fork's wider "might be the player's" test (any game's `game/saves`,
  host paths compared case-insensitively, a HOME whose Ren'Py folder links
  into the host tree) applies to what a running game exposes, the policy
  gate, import destinations, and reports. What a launch request gives up,
  and what a `renforge_jump`, `renforge_new_game`, or Warp relaunch inherits
  from the game it replaces, use 0.7.6's narrower test, so neither hands out
  more of the player's storage than 0.7.6 does for the same arguments.
- `renforge_session` `save`, `load`, and `list` act only on a game whose
  saves and HOME are in RenForge's own folders (the state folder or a
  session root, by what its bridge reports). Any other running game, such as
  one `renforge_launch` started on the player's saves, is refused with
  `SESSION_NOT_ISOLATED` before a slot is written, loaded, or listed, so
  these actions keep their promise of never touching the player's saves.
  `renforge_session(action="launch")` is refused the same way, before
  anything starts, when its saves folder in the state folder, or any folder
  on the way to it, is a link, even one to another folder there. So are
  `renforge_launch`, `renforge_jump`, `renforge_new_game`, and the Python
  launcher for a `savedir` path that passes through the state folder,
  judged as the launcher resolves it, so through a link to that folder or a
  `..` after a link too (phase `preparing_isolation`).
- `renforge_validate` (0.7.5) is in the `project` toolset.
- Write receipts (0.7.4) for files in the state folder (captures, scene
  snapshots, the activity log's `files_touched_file` sidecar) are absolute
  paths, since that folder is outside the project.
- The `files_touched` receipts of a `renforge_saves` save (the savedir) and
  of a `renforge_saves` or `renforge_session` import (the copied save files)
  now use `/` on Windows too, like every other receipt; `savedir` keeps the
  platform's own form.
- `renforge_session(action="import")` returns a `files_touched` write
  receipt and passes on the import's own failure codes; a missing slot is
  `NO_MATCHING_SAVE_SLOTS`. It copies the slot to where the running game
  keeps its saves; a running game whose saves are in a folder this server
  did not create is refused with `SESSION_SAVES_ELSEWHERE`. A game this
  server did not start whose bridge does not answer (busy, or still
  starting) gets a retryable error and nothing is copied, instead of the
  slot landing in a folder the game does not read.
- `renforge_autopilot` always launches isolated; `RENFORGE_ISOLATION=existing`
  gave it the player's saves, and it takes no `authorize`.
- Tools that need a running game say so when none is: `code="NO_RUNNING_GAME"`
  with a pointer to `renforge_session(action="launch")`, or `GAME_STARTING`
  while it starts. Both were reported as "bridge metadata failed validation".
- `renforge_act` on an id the screen does not list adds a `message` and the
  current observation to `error="missing"`.
- `renforge_info` lists the server's `toolsets` and, when `play` is listed,
  the play loop's steps; it leaves out the Live Editor block unless both the
  `editor` and `live` toolsets are listed, since its workflow launches with
  `renforge_launch`. The server instructions likewise name only the
  listed tools (the Live Editor too only with both), and say what each
  `forward` value asks of the agent.
- The `renforge_observe` description leads with `forward` and what to call
  next. It said the PNG's SHA-256 is `frame_id` (it is `frame_hash`) and that
  keyboard shortcuts are not inventoried (`keys` lists them). Compact
  observations drop the top-level `frame_id`, which `frame` carries.
- `docs/MCP.md` opens "Look, then act" with the loop and a `forward` table,
  and describes chrome as implemented; the README's gameplay entry describes
  the play tools instead of the earlier `act` arguments.

- RenForge keeps its per-project state outside the game folder, under
  `RENFORGE_STATE_DIR` or `~/.local/state/renforge/projects/<name>-<hash>/`.
  Opening or launching a project no longer creates `<project>/.renforge`.
  On a case-insensitive volume every spelling of a project path shares one
  state folder, one bridge lock, one dashboard and one owned session, so a
  game launched through one spelling is this server's own through another.
  A Live Editor save, undo or rollback of a game on another filesystem than
  the state folder (a second drive) stages its exchange files in
  `.renforge/editor-exchange/` on the game folder's own filesystem instead
  (under the project, or, for a game folder mounted from elsewhere, under
  that mounted folder), because the atomic exchange cannot cross
  filesystems, and removes them when it is done; a
  conflict keeps them as evidence.
  `RenpyProject.cache_dir` is gone; `renforge.state.project_state_dir`
  names the folder.
- Launches in a Wayland session use `SDL_VIDEODRIVER=wayland,x11` so games
  start while the screen is locked. The startup deadline defaults to 180 s and
  reads `RENFORGE_STARTUP_TIMEOUT`.
- Sessions with isolated preferences start with transitions off
  (`RENFORGE_TRANSITIONS=on` keeps them). `observe` and `act` return a
  compact observation (`renforge_explain(id=...)` has a control's full
  record), and `act` accepts `observation_id` alone as its guard.
- Observations report `seen` (the current statement already finished once
  since launch), and repeated lines in `crossed` carry `seen: true`, so an
  agent can tell when it is looping.
- Text inside a scrolling viewport (a phone task list) is readout, not the
  viewport's label. A game displayable that keeps its Ren'Py Actions in one
  `*click*` attribute (Academy34's `click_action`) is a control rather than
  unknown input. Menu choices without a focus rectangle (clipped, or in a
  transformed viewport) report the `source` of their button.
- Bounded skipping is about 7x faster on a standard GUI (138 to 19 ms per
  line; Stranded in Space 185 to 15 ms). Launched games run without vsync
  (`RENFORGE_VSYNC=on` keeps it), a game's `config.skip_delay` is set to 0
  during an operation and restored after, each line is checked once, the
  host polls a cheap operation status instead of full observations, and the
  geometry walk avoids Ren'Py's rollback-tracked containers.

- The play loop has one tool per task, in the `play` toolset:
  `renforge_session` (launch, status, save, load, list, stop with agent-mode
  defaults), `renforge_observe` (always compact), `renforge_act`
  (`observation_id`, a control id, `key/<name>`, or nothing to continue, plus
  one `value` read by the control's type; it always progresses to the next
  decision), `renforge_explain` (now with a control's full record), and
  `renforge_mod`. The fork-only `renforge_advance_until` tool and `act`'s
  `interaction`, `session_id`, `text`, `key`, `hold`, `x`, `y`, `drop`,
  `submit`, `observe`, `advance`, `max_steps`, `mode`, and `detail` options
  are gone; the `live` toolset keeps every earlier upstream tool.
- `renforge_explain` reads playthrough state as data: `variables=[...]` reads
  store variables and dotted fields, and explained labels, screens, and
  controls carry `values` for the variables their conditions read. Values
  skip loop variables and attribute roots, show enums as `Day.SUNDAY`, and
  read class-level defaults. A long list or dict says it is cut short
  (`size`, `truncated`) instead of silently dropping entries. `variables`
  is passed alone (with another selector it is refused) and reads at most
  100 names, saying `truncated` when given more. A dotted path never steps
  into a module or a private (`_`) field after its first name, so
  `renpy.os.environ` (the game's environment) cannot be read.
  `explain(source=...)` on a menu, `if`, or `while` lists what each branch
  does, and label and screen entries name their file only when it differs
  from the result's own. Search lists a screen's matches in source order
  with whole snippets.
- Acting with an observation that is slightly out of date is accepted when
  only unrelated parts of the screen changed (a notification closing) and the
  target control is unchanged. Menu captions are read without text tags,
  `show text` and other image-referenced text reaches readout, inputs report
  their typed `value`, and state-only actions (`SensitiveIf`, `SelectedIf`)
  no longer keep menu buttons out of chrome.
- A menu choice the game's choice screen draws as a button with no action
  (Total Degeneracy Island's `disabled=` items) is reported disabled and
  `act` refuses it. It used to look enabled and `act` ran the hidden choice.
- A hard pause no longer counts as a showing say window, so free roam that
  waits on one (Total Degeneracy Island) stops repeating the last scene's line as
  `dialogue`.
- `renforge_session(action="load")` returns the restored game's observation,
  so the next `renforge_act` can use it directly. Its old `restored_label`
  named the label from before the load, because the bridge replies before
  the engine restarts into the save.
- `renforge_session(action="launch", label=...)` starts the story at a label
  for testing and modding, also in archive-only games; observations carry
  `started_at_label` because earlier story state never ran. The label is
  applied only at startup, so such a launch always starts a fresh game: one
  this server runs is replaced, one started elsewhere is refused.
- Play sessions keep their saves across launches in the project's RenForge
  state folder. Continuing skips through pauses, clicks past a line that only
  has optional buttons beside it, ignores HUD buttons on `show screen` screens
  during clickable lines, and stops with `stop="loop"` on a line it already
  passed; a time-budget stop is no longer reported as a failure.
- `RENFORGE_TOOLSETS` lists only chosen tool groups (`play`, `live`, `debug`,
  `editor`, `mod`, `project`, `translation`, `build`, `docs`); it replaces the
  fork-only `RENFORGE_TOOLS` filter.
- Lint, compile dumps, translation runs, and shadow lint run Ren'Py with a
  disposable HOME and one disposable save location. Ren'Py writes a game's
  persistent data on every start, so these used to rewrite the player's
  `~/.renpy/<game>/persistent` and `game/saves/persistent`. RenForge's
  save and JSON-dump adapters ship as `.rpe` archives, so Ren'Py 8.0-8.2,
  which ignore `.rpe.py` files, load them too.
- Lint reads Ren'Py 8's `file.rpy:12 message` lines. `renforge_mod(action="lint")`
  used to report every mod clean.
- `renforge_mod(action="reload")` takes no mod name: it reloads every script,
  wherever the mod lives.
- A viewport, or a scrollbar Ren'Py built for one, is marked `scroll` and is
  never a decision: a chat log shown with a line (Come Home's livestreams) no
  longer stops `act` at every line. A slider bound to a value is still input.
- An observation's `label` is the label whose block holds the running
  statement. It used to be the last label entered, which went stale after a
  `call` returned (a hub reported `_call_determinehinttext_2`) and after
  loading a save whose `after_load` calls other labels. `explain` search no
  longer puts a top-level `define` inside the label above it.
- Speaker names drop Ren'Py text tags like the line itself, so a nameless
  voice written `{noalt}{/noalt}` (Ravager) has no `who`. Readout no longer
  repeats the say window's name and line, which `dialogue` already carries.
- A line paused with `{w}` no longer stops `act` with `stop="loop"`: its
  later segments come from the same run of the say statement. A loop is the
  statement running again with the same words.
- Controls carry the game's `tooltip` text, often the only words an
  image-only button has (Come Home's battle icons).
- `input_candidates` lists only handlers on screen. Hidden or one-pixel ones
  (Ravager's gamepad listeners, a key listener) have nothing to point at.
- A bar that only shows a value (`bar value hp range max_hp`, which Ren'Py
  leaves unfocusable) has no operations and never holds a line.
- When a game turns skipping off (`_skipping = False` in Come Home's
  battles), `act` clicks through each line instead of stopping with
  `unsupported`, clicking again if the line is still up 0.3 s later.
- A button with a `focus_mask` (Mythos's suspect board: full-screen buttons
  each masked to one photo) reports the region a click reaches and a proven
  `click_point`, found by one grid of engine hit tests per interaction.
  Before this, most photos were unclickable with whole-screen bounds.
- An input's text is its prompt as drawn: `[mc_nick]` substituted, tags
  removed. Image names keep their tag (`lorbook naughtydn`, not `naughtydn`).
- A button's label skips text under a transform at alpha or zoom 0, which
  Ren'Py doesn't draw. Heaven's Door's menu read "Start New Content! Start"
  because each button holds a hidden hover card; it now reads "Start".
- A plain `pause` with no line (a location card in a cutscene) is clicked
  past like a line when the only buttons on screen are kept up from earlier
  (a HUD), the first time it comes up; a pause the game returns to is still a
  hub that stops as a choice. Screens restored by loading a save count as kept
  up, so a HUD no longer stops the first line after a load. Found in Artifice.
  The hook is re-added if a game assigns its own `config.after_load_callbacks`
  list (IA2), and a restored screen Ren'Py rebuilds before the first
  interaction still counts by name.
- A launch whose game stops on Ren'Py's error screen before the bridge comes
  up (an exception during init) fails within seconds with
  `GAME_STARTUP_ERROR` and the game's `traceback.txt`/`errors.txt` report,
  instead of waiting out the 180 s startup timeout. Astreon and Driven by
  Desire ship stale `.rpyc` files whose first recompile raises a duplicate
  translation error. When Ren'Py compiled changed scripts during that launch,
  it is retried once (the next launch loads the fresh `.rpyc` files and starts)
  and the ready result carries `startup_warning` with the first error. Four of
  the first ten games swept (Astreon, Driven by Desire, Eden, Found in
  Translation) only started this way. A game whose presplash has transparent
  corners (a shaped window, which SDL's Wayland driver cannot create) exited
  at once; it is now launched again with X11 (Inanna's Revenge, Island
  Echoes; on Ren'Py 8.5 the error carries no message, Apocalypse and
  Bunkered with Femboy). Launched games also get an editor that
  opens nothing, so a crash no longer opens `traceback.txt` on the user's
  desktop.
- The injected bridge and Live Editor no longer read builtins or their own
  imports from the game's store. Eternum defines `id` as a Character, so every
  observation failed with "Character expects its what argument to be a
  string"; a game's `default types = [...]` would also have replaced the
  bridge's `types` module, and the bridge's imports overwrote game variables
  with the same names. The injected copy binds each to a private `_rfb_` name
  (`renforge/bridge/guard.py`) without changing line numbers. Names Ren'Py
  itself rebinds in the store (`eval`, `str`, `open`, `print`, `input`, and
  the revertable containers) keep their store meaning, and the Live Editor's
  imports stay store names because its screens use them. The builtin aliases
  are set on the `builtins` module rather than in the store:
  `renpy.reload_script()` wipes the store under the bridge's listener thread,
  so a Live Editor save that reloads the script killed the listener with
  `NameError: _rfb_min` and the bridge refused every later connection.
- A click sound does not make an interface button a story decision. Astreon's
  main-menu buttons run `Play("sound", ...)` with `ShowMenu("load")`, `Quit`,
  or `OpenURL`, so they were gameplay controls beside the real Start button;
  they are chrome now. Nested action lists (`action [Play(...)], OpenURL(...)`)
  are flattened as Ren'Py runs them.
- A button that shows or hides a screen alongside an interface action (Love
  Sucks: Night Two's fast-forward button, `Show("fastHide"); Skip()`) is
  chrome. `Skip()` turns sensitive only while skipping is allowed, so during
  progression the button became a decision and stopped every continue at
  once, though the observation afterwards said `forward="dismiss"`.
- `crossed_truncated` is true only when events were lost: one failed event
  poll no longer marks the transcript short when the next poll resumes from
  the same cursor.
- `label` names the label a story line falls through from when it sits below
  an empty-block label (`label start:` with the story unindented beneath, as
  in Found in Translation). Observations read `after_load` after a load there,
  and explain search gave such lines no label.
- An image button whose image is a dynamic image reports the image it
  resolved to (`player1_avatar_idle`), not its `[player_skin]_avatar_idle`
  template. Found in Inanna's Revenge.
- An `act` refused because the game is mid-pause reports
  `observation_status="ready"` with the current observation and a hint to
  observe again, instead of `timeout`; the refusal is immediate and nothing
  timed out (Eden's splash screen).
- A choice button whose action hands its menu item to a game callback
  (`action Function(menu_choice_intercept, menu_item=i)`, Light of my Life) is
  that item: one control per choice, with the item's caption and the button's
  bounds, and choosing it runs the game's callback. Before, each choice was
  listed twice, once as a button and once as a bound-less `choice/item/N`.
- A focus-masked button on its own modal screen is no longer reported as
  covered. Nightmares of the Moon's start menu (`call screen`, modal) holds two
  full-screen masked imagebuttons; probe points the mask leaves open fall to
  the modal screen, which read as "covered by a modal", so `act` refused both
  and the game could not be started. Only a modal screen above the button
  covers it now; the focus-mask scan finds where it can be clicked.
- A 7.5/8.0 dual build that ships only Python 2 runtimes (`lib/py2-*`) is
  its 7.x engine and is refused like other pre-8 games. Special Harem Class
  was launched on 8.0.3 and stopped on Python 2 syntax errors. A Python 2
  build's `version = u'7.7.3...'` is read too (Price of Power had no detected
  version). `GAME_STARTUP_ERROR` messages quote the error line of either
  report kind, not the trailing date.
- A line in a subroutine that the story calls from different places is not a
  loop. Secrets and Promises says "*Moans*" from one `Moan` label that every
  scene line calls, so `act` stopped with `stop="loop"` every few lines; the
  loop check now includes where the line was called from.
- A short pause no longer reads as pending for longer than its delay after
  the bridge first sees it. Its start time follows Ren'Py's `interact_time`,
  which restarts with the interaction; a game that restarts it repeatedly
  kept a 1 s pause "unstable" until `act` ran out of time (Secrets and
  Promises).
- Screenshots are bounded to 384 KiB about 5x faster (2.7 s to 0.6 s for a
  1080p frame): PNGs are encoded without `optimize` and resized once to an
  estimated size instead of in 3/4 steps. A time-budget stop reserves 2 s for
  its final screenshot and transcript, and that encode used to take longer,
  so `act` returned no final observation and `crossed_truncated=true`
  (Nightmares of the Moon's unskippable intro).
- The final observation of `act` settles on a screen that keeps changing
  within one stable interaction (a music room whose timer rebuilds its
  buttons, Desert Stalker). It waited for two identical observations that
  never came, so each act took the full 30 s and returned no observation.
- When `act`'s budget ends inside a pause or transition, its final
  observation is that current state (`stable=false` with `unstable_reason`)
  instead of none with `observation_status="timeout"` (Nightmares of the
  Moon's intro, with a 2 s hard pause at the end of the budget).
- A line that stays a "pause" longer than any short pause can last (6 s) and
  that the player could click past is clicked once before progression starts.
  In Secrets and Promises a `{w=1.0}{nw}` line stayed pending for a whole
  30 s act, though one click moved it on.
- Ren'Py's parse-error screen is a `game_error` too (its first error line), and
  a launch that wrote `errors.txt` fails with `GAME_STARTUP_ERROR` even though
  the bridge came up, so a recompile that clashes with archived scripts is
  retried (Fleeting Memories: "label ... is defined twice").
- A game exception is reported, not shown as one more screen. While Ren'Py's
  error screen is up, observations carry `game_error` with the exception line
  and traceback file, progression stops with `stop="error"`, and `act` hints
  at Rollback, Ignore, or Quit. Before, Love Sucks: Night Two's error screen
  read as an ordinary choice and continuing said only "waiting for one of its
  controls".
- The bridge's runtime objects derive from the plain `object`, not Ren'Py's
  store `object` (`RevertableObject`). Their changes went into the rollback
  log, so a save could try to pickle the bridge's queue: "cannot pickle
  '_thread.lock' object ... requests.mutex" (Stranded in Space).
- A screen's `key "mousedown_1" action ...` (a click anywhere) is listed as a
  control that runs its action. Stranded in Space's laptop tutorial advances
  only so; it read as `forward="none"` with nothing to act on, and act's
  `key/` pseudo-controls press keyboard keys only.
- `explain` writes a Ren'Py 8.0-8.2 screen's parameters as source
  (`(items, title="")`), not `<renpy.ast.ParameterInfo object ...>`.
- A server whose toolsets include neither `debug` nor `editor` launches games
  whose bridge refuses `eval` and `set_var` (`eval_disabled`), so a play-only
  server offers no way to run code through the bridge.
- Coverage probes stop at the first point that reaches a control (usually its
  center), and the focus-mask grid uses 700 hit tests instead of 3000. On
  Mythos's suspect board an interaction's probes fell from 3047 to 835
  (0.16 s to 0.04 s) with every photo still found.
- Action summaries name a mangled common-library class as written:
  `MusicRoomPlay("...")`, not `_m1_00musicroom__MusicRoomPlay("...")`.
- When nothing else moves the game on, interface buttons on the
  interaction's own screen are its choices. The Power of Truth opens with a
  called disclaimer whose buttons are `MainMenu()` and `Quit()`; both were
  chrome, so the observation had no controls and `forward="none"`. A HUD kept
  up, the quick menu, and Ren'Py's game-menu screens keep such buttons as
  chrome.
- A launch that times out, or whose game exits before the bridge starts,
  reports the last lines of the log.txt it wrote as `details.log_tail`. A game
  that quits on its own platform check says why only there. A log that stops at window or GL setup (`Windowed
  mode.`, `Maximum texture size`, a shader cache line) means the window never
  opened, as with an X11 window while the screen is locked.
- `renforge_wait_until` with an `expr` condition is open-world under
  `RENFORGE_POLICY=enforce` and takes `authorize`, like a scenario `wait`
  step; it was unmanaged, so enforce mode ran its Python unasked.
  `renforge_set_var` is classified mutating. A session launch always uses its
  own HOME, even when `RENFORGE_ISOLATION=existing`.
- Launches that reach the player's saves are classified as destructive more
  reliably: host save paths match case-insensitively (on Windows and macOS
  `~/library/renpy` is the player's `~/Library/RenPy`), and a custom `home`
  whose Ren'Py save folder resolves into the player's tree counts too, as
  does a save path inside any game's `game/saves`, not only this project's.
- A reply that times out while the game's main thread is busy (entering a
  scene that decodes large images) no longer ends `act`
  with `stop="timeout"`: progression and the final observation ask again
  while budget remains.
- `renforge_mod` moves to its own `mod` toolset. Writing and reloading a mod
  runs game code, so a server limited to `play` can no longer run code in the
  game; `RENFORGE_TOOLSETS=play,mod` keeps modding.
- Observations list the key bindings of the screens shown as `keys`: the
  binding, its action, and the `key/<name>` that `act` can send. A settings
  screen that continues only on `key "K_SPACE" action Return()` said so only
  in its text ("Press SPACE to continue"). Interface and do-nothing bindings
  are left out, and bindings never count as decisions.
- `act` with a letter key now types that letter, as a keyboard does. A
  quick-time event bound to `key "d"` matched only the typed character, so
  `key/d` never fired it. Such bindings, and `keydown_K_d`, now report
  `key/d`.
- A button whose click does nothing (`NullAction()`, or only a sound or a
  selected/sensitive state) is `inert` and never a decision, so a `pause`
  beside a tutorial's decorative icons is clicked past instead of stopping
  with `forward="choose"`.
- Continuing a line that a modal screen holds (a stats page opened from a
  HUD) names that screen in `hint` and says to close it. The hint was
  missing whenever the refused click past the line left a reply behind.

### Added

- `renforge_session(action="import", slot=...)` copies one of the player's
  save slots into the session's saves (the player's file is only read), and
  `list` shows them as `player_slots`. The running game names its save folder,
  so this also works for games shipped only as archives. The mod guide now
  points here instead of `renforge_saves`, which the `play` toolset lacks.

- Runtime compatibility checks reject games/SDKs below Ren'Py 8.0 with
  `UNSUPPORTED_RENPY_VERSION` before download, injection, or launch. Explicit
  SDK overrides do not bypass a legacy game's engine requirement.
- With `version="stable"` (the default) the SDK follows the game's own
  engine: a `renpy_version` pin in the project's `renforge.json`
  (`{"renpy_version": "8.3.7"}`, a file Ren'Py never reads, for source
  projects), else the shipped engine's version, else
  `game/script_version.txt`. A `version` that is neither `stable` nor
  `X.Y.Z` returns `INVALID_RENPY_VERSION`.
- Engine-based ending evidence in gameplay observations and progression:
  `stop="end"` confirms a root script return at the main menu; `main_menu`
  distinguishes initial menus and explicit restarts. Evidence persists until a
  new run, and confirmed endings emit a cursor-readable `ending` event.
- Every pull request now runs the broad Ren'Py 8.5.3 SDK integration suite on
  Linux/Xvfb, with SDK caching and an explicit diagnostic artifact allowlist.

### Changed

- Simplified shared discovery: owning screen instances supply named widget IDs,
  and the collector assigns IDs/facts once for observation and actions. Removed
  obsolete discovery helpers and scene placement calculations; host
  progression lives in `progression.py`.
- Updated gameplay/tool guidance for clipped menu entries, incomplete discovery,
  content-based frame guards, and process-exit status.
  Historical harness proposals are distinguished from current TODOs.
- Unified gameplay and legacy UI discovery around one control collector and
  canonical IDs. Engine menu actions expose clipped choices on custom layers;
  ambiguous captions are refused and selections accept observation guards.
- Gameplay images preserve source frame identity and return a frame descriptor
  for guarded clicks on resized images, independently of the delivered PNG hash.
- Scene geometry uses completed renders and composes transforms/clipping.
  Repeated placements retain render paths; alpha inspection uses cached renders.
  Bounded focus probes find usable points on partially covered/masked controls.
- Unsupported custom event handlers are explicit input candidates and stop
  progression. Conditional custom eligibility remains unknown.
- Owned process termination distinguishes clean exits from nonzero exits while
  preserving historical observations and leaving story completion unconfirmed.
- Observations wait for active screens to appear in the completed render;
  progression boundary reports agree with the settled final observation.
- Coordinate clicks on unfocused custom displayables now allow hover to render
  before mouse-up, retain synthetic cursor state until delivery, and clear it
  before replying. A bounded lease and interaction guard prevent delayed clicks
  from starting after the scene changes.

- Session storage initialization handles an absent save scanner thread and an
  absent Emscripten flag while preserving the single isolated save location.
- Progression uses bounded native skip, with interaction-level guards,
  time/step leases, temporary unread-text and presentation preferences, and
  guaranteed restoration while the engine is responsive. It stops before
  actionable UI.
- Standalone progression accepts `screenshot=false`. Gameplay PNGs are bounded
  for MCP transport and report scaling metadata with hashes of returned bytes.
- Frame timeouts, transport failures, verified runtime errors, and uncertain
  action outcomes are distinct. Final observations settle after skip stops,
  stale actions return fresh snapshots, and events are drained during progression.
- Physical `act` keys include Tab; speaker names and callback dialogue use
  engine interpolation.

- Host and injected bridge now share one gameplay contract for IDs,
  classification, text cleanup, operations, and guards. Discovery reuses one
  bounded scene inventory, and action guards reuse its widget references.
- `renforge_act` returns the next stable observation, guarded by the
  observation it acts on. Action success remains separate from follow-up
  status.
- Custom overlay hubs remain gameplay controls. Held keys can be released after
  interaction changes; viewport axes are validated before mutation; drag
  capabilities and target eligibility are checked before callbacks run.
- Crossed dialogue preserves repeated occurrences and reports event-buffer gaps;
  rendered text replaces older-engine callbacks containing source interpolation.


- Autopilot now identifies choices from active Ren'Py menu `items`, including
  custom-named and one-item menus, without inferring narrative intent from
  arbitrary focusable screen controls.

### Fixed

- `renforge_mod` writes work when the project path goes through a symlink
  (a linked games library, macOS `/var`, `/home` -> `/var/home`); only links
  under `game/` are refused. `write` no longer takes over a file it did not
  create: a `game/mods/<name>/` folder without a manifest, or a file (or a
  `.rpy`'s `.rpyc`) the manifest does not list, is refused, because `remove`
  would then delete it. The manifest name is refused in any case or folder,
  removal uses the same junction-aware link check as writes, and `package`
  skips an invalid manifest entry under `missing` as `list` and `remove` do.
  `remove` refuses a mod whose own folder, `game/mods` or `game` is a link,
  instead of deleting the manifest and empty folders in the link's target,
  and never enters a linked folder (a Windows junction) inside the mod.
- `renforge_mod(action="lint")` lints a temporary shadow copy, as
  `renforge_validate` does, so it no longer writes `.rpyc` or cache files
  in the game. It lints with the game's own engine version, as
  `version="stable"` does elsewhere (the `renforge.json` pin, the shipped
  engine, then `script_version.txt`), never an SDK found in the project.
  It keeps Ren'Py parse errors (`File "...", line N:`), passes on
  `timed_out` and a bounded `raw_tail` when it fails with nothing in the
  mod, and reports an unknown mod instead of linting the whole project.
- `renforge_mod` `write`, `remove`, and `package` return
  `files_touched`/`files_deleted`, so the activity journal records them
  (the packaged zip as a POSIX path, like every other receipt), and
  `reload` returns a compact observation like the other play tools.
- `RENFORGE_TOOLSETS` is parsed once: a value naming no group (`,`) or a
  mis-cased name stops the server, instead of listing no tools while
  `renforge_info`, the instructions, and the bridge eval switch described
  every group (the eval switch fails closed). `renforge_info` is listed by
  every server, and `renforge_estimate_translation` (an image-shift
  estimate) moves from `translation` to `live`.
- `renforge_mod` is annotated `destructiveHint=true` and
  `openWorldHint=true`, and `renforge_session` `openWorldHint=true`, matching
  their riskiest actions.
- `renforge_capture_screenshot` failed with a `ValueError` since captures
  moved to the state folder outside the game. Its `relative_path` and
  `files_touched` receipt now name the capture's absolute path there, as
  the activity log's `files_touched_file` does.
- `renforge_set_var` and the dashboard's set-var refuse a name that is not a
  Python identifier. The bridge stored `""` or a dotted name such as
  `_preferences.skip_unseen` under that literal name, changed nothing the game
  reads, and reported success. A scenario `set` step now fails when the write
  is refused, and an `advance` step fails on a count outside its schema
  (`"3"`, `0`, `2.9`) instead of advancing once.
- The dashboard answers malformed requests with a 400 naming the field instead
  of acting on a coerced value: a boolean, zero or negative launch `timeout`
  (or an integer too large to be a deadline, which was a 500),
  an unknown `display`/`audio`, a non-boolean `cleanup_on_stop` on `/api/warp`,
  a non-string `expr`, a boolean or negative screenshot size, a choice `index`
  that is not an integer or an empty choice `text`, an unknown control action,
  and a translation `language` that is not a name. `{"warp": ""}` on
  `/api/live/launch` no longer restarts the running game.
- `renforge_generate_translations`, `renforge_translation_stats` and
  `renforge_export_dialogue` refuse a `language` that is not a Ren'Py language
  name with `LANGUAGE_INVALID` before running Ren'Py, which joins it into
  `game/tl/<language>/` unchecked, so `../..` wrote translation files outside
  the project. The dashboard's translation routes use the same check.
- Play loop: continuing clicks past a line held by a pause that never ends
  (the bridge refused that click as unstable) and keeps to the caller's
  time budget. A speaker defined as `character.e`, quoted, or a dynamic name
  is reported as `who`; a soft pause no longer repeats the last line as
  `dialogue`; words stay apart across `{p=N}`, `{space=N}` and `{vspace=N}`.
  An `Input` with `length=0` takes any length, an input callback's error is
  reported instead of success, a hover lands where a click would, a queued
  `load_slot` reports the load once, and the ending is confirmed at the main
  menu's first interaction even before anything observes. A game's own
  subclass of an engine bar, viewport, drag or button is classified like
  the engine widget.
- Autopilot ends a run when the bridge refuses a choice and lists it under
  `choice_errors`, instead of counting the menu as explored.
- A `save` or `load` slot that is a path rather than a slot name (absolute,
  with a drive, a `/` or `\` separator, a `:` (on Windows `page:name`
  writes a stream of the file `page`), or `.`/`..`) is refused with
  `SAVE_SLOT_INVALID` before the game is asked, in `renforge_saves`,
  `renforge_session`, scenario steps and the dashboard. Ren'Py joins the
  slot onto its save folder, so such a slot reached any folder; a
  `renforge_session` save, which needs no `authorize`, could write into the
  player's own saves. The bridge's `save_slot` and `load_slot` refuse such a
  slot themselves too, with the same code.
- Project scans (`renforge_scan_project`, `renforge_assets`,
  `renforge_find_references`, and the dashboard's script list, story map and
  translation strings) no longer walk into a folder under `game/` that is a
  Windows junction and read the scripts in the tree it points to; symlinked
  folders were already skipped. Stopping a session likewise lists a
  junction in its session root without walking the tree it points to.
- Autopilot's `autopilot.json` progress report is written without
  following a link at the file itself; one planted there is refused.
- `renforge_mod` `write` records a new path as pending, with a digest of
  its content, before writing it, and lists it once the content is on
  disk. A file that appears at that path after a failed write is no longer
  taken over by a retry or deleted by `remove`; a file published before the
  last manifest update failed is listed as `pending`, finished by a retry,
  and deleted by `remove`.
- The play loop's stuck-pause click is timed from the pause's first frame:
  a long transition before a short pause no longer makes that pause look
  stuck and clicked the moment it appears.
- `renforge_session` `import` refuses a running game whose bridge cannot
  report its save folder with `SESSION_SAVES_UNKNOWN`, before copying
  anything. It used to treat that game as no game, copy the slot into the
  session's saves folder, and say to load it, which that game could not.
- `renforge_session` `load` attaches the restored game's observation only
  when it is stable; at the wait's timeout it returns `next` (observe, do
  not load again) instead of an unstable observation offered for
  `renforge_act`. `renforge_mod` `reload` reports `settled: false` and
  `next` when the screen had not settled, keeping the reload's result.
- Continuing with `renforge_act` and no control holds its first
  observation, the click past a line, and the wait after that click to
  the step's own `timeout`, so a short timeout is no longer overrun before
  progression starts. A click accepted in time is still reported.
- Project scans and the dashboard's script list no longer walk a starting
  folder (`game/`, `game/tl/<language>/`, a mod folder) that is itself a
  symlink or junction, or whose link state cannot be read; they list
  nothing from it. A folder listed without search permission is skipped
  instead of aborting the whole scan.
- The launch guidance describes the HOME a launch resolves to:
  `savedir=temporary` isolates HOME only while `home` is omitted or `auto`
  (`home=existing` keeps the user's HOME and needs `authorize` under
  enforce), and under `RENFORGE_ISOLATION=temporary` a `savedir` path with
  `home` omitted gets a temporary HOME and needs no `authorize`. The
  `savedir` parameter, the launch policy and `authorize` texts, and the
  server instructions said otherwise.
- A dashboard stays discoverable after its project folder is renamed only
  in case on a case-insensitive volume: a client started after the rename
  accepts the record's earlier spelling of the same folder. Relative paths
  and paths that lead to another folder are still refused.
- A game this server runs keeps its bridge lock, metadata and session key
  after a case-only rename of its folder even once the bounded spelling
  cache fills and is cleared: the spelling it started with is pinned while
  the session lives.
- A launch with `display="none"` is refused with `DISPLAY_UNAVAILABLE`
  also when a running game would be reused; only a fresh launch refused it
  before, and reuse reported success for a launch that could never start.
- `RENFORGE_TOOLSETS` with an unknown name beside `all` (`all,projecct`)
  stops the server at startup like any unknown name, instead of listing
  every group.
- `renforge_validate`'s schema admits `checks` only as `["lint"]` (or
  omitted or `null`), as its description says, and its `timeout` text
  states the accepted 0-600 range instead of promising to clamp values the
  schema refuses.

## [0.7.6] - 2026-11-18

### Added

- Opt-in launch isolation for `renforge_launch`, `renforge_jump`,
  `renforge_new_game`, and dashboard launches. Pass `savedir=temporary`, or
  set `RENFORGE_ISOLATION=temporary` for every launch of a server; launches
  that don't pass `savedir`, `home`, `persistent` or `preferences` use the
  game's normal saves, as before. An isolated session gets a disposable save
  directory (native `--savedir` plus `RENFORGE_SAVEDIR` /
  `RENPY_PATH_TO_SAVES` / `RENPY_MULTIPERSISTENT`), a private HOME/XDG/temp
  tree (new `home` knob), empty persistent data, and reset preferences (new
  `preferences` knob). Ready payloads include `session_id` and an
  `isolation` object so an agent can see the mode. A launch is refused with
  `SESSION_ISOLATION_MISMATCH` rather than handed a running game that uses
  the user's saves or HOME where the launch isolates them (each checked
  separately), including one started elsewhere (another RenForge process or
  the dashboard). An external game's HOME counts as isolated only when it
  reports one inside a RenForge session root, and its saves only when every
  active save location it reports is inside one; anything else, including a
  bridge that cannot report them, counts as the user's. A reused external
  game's ready and status payloads carry what it reported as `isolation`. A
  launch whose state modes a fresh launch would reject (`empty` without
  `savedir=temporary`) fails with `LAUNCH_STATE_MODE_CONFLICT` before reuse.
  A `renforge_jump`, `renforge_new_game` or dashboard Warp relaunch whose
  isolation knobs are all omitted or `auto` keeps the saves and HOME
  isolation of the game it replaces (resolved by the process that owns the
  game, unless this server's `RENFORGE_ISOLATION=temporary` already isolates
  it); it gets a fresh session, so imported slots are not carried over.
  Mode tokens (`auto`, `existing`, `default`, `temporary`, `empty`) are
  matched case-insensitively, so `savedir=Temporary` isolates instead of
  creating a folder of that name. This is application-layer isolation, not
  an OS sandbox. (#125)
- `renforge_saves` gains `list_user` (observational host listing) and
  `import` (copy selected host slots into the savedir of a session launched
  with `savedir=temporary`, never into a `savedir=<path>` folder; never
  bind-mounts the user tree). `list_user` filters by `pattern`, a glob
  matched against the whole slot name; `import` selects by name, by
  `pattern`, or both; `list` keeps its `regexp`, which Ren'Py applies.
  `pattern` and each slot name are capped at 256 characters and `slots` at
  1000 names (`SAVE_IMPORT_INVALID`, checked before anything is listed).
  Both read only the folder that the game's `config.save_directory` names
  directly under the Ren'Py save root, plus a `game/saves` inside the
  project; they open the game's scripts without following links or
  blocking, skip one over 4 MiB, and list before an import holds the
  session, so a stop never waits on a listing. A save folder swapped for a
  link lists nothing, and `list_user` writes nothing, not even `.renforge`.
  Import resolves none of the session's own folders, so a session root or
  savedir swapped for a link is refused with `SAVE_IMPORT_FAILED` instead of
  followed, and never recreates a stopped session's folders. On every
  platform, folders are listed and slot files read and copied at handles
  that do not follow links or junctions. A slot file import will not copy
  (a link, a FIFO or other non-regular file, one over the size cap, or an
  unreadable one), or a slot whose host folder was swapped for a link, is
  listed in `refused` with a reason, never dropped; a slot counts as
  imported only when its save file was copied, and an import that copied
  nothing fails with `NO_MATCHING_SAVE_SLOTS`.
  Import returns a `files_touched` write receipt (absolute paths of the
  copied files, also on `SAVE_IMPORT_FAILED`). Its other codes are
  `NO_ISOLATED_SESSION`, `SAVE_IMPORT_NOT_ISOLATED` and
  `PROJECT_PATH_UNAVAILABLE`. A stop waits for an import in progress, and an
  import never starts on a stopped session or one whose game has exited.
  Copied slots are removed with a temporary savedir on stop, unless
  `cleanup_on_stop=false` or the cleanup fails. A failed save rescan is
  reported as `rescan_error` instead of leaving a stale slot list
  unexplained. (#125)
- `renforge_saves(action="import")` reaches an isolated session that the
  dashboard launched: the request goes to the dashboard that owns the
  session (new token-checked `POST /api/live/saves/import`), and that
  dashboard's answer, code and shape included, is final; only an unreachable
  dashboard gives `DASHBOARD_REQUEST_FAILED`. The MCP side waits up to 300 s
  for a delegated import (launch and stop keep 45 s). (#125)
- `language` composes with isolation. The "changed the stored preference"
  warning follows the resolved save directory, so an isolated launch (or
  `RENFORGE_ISOLATION=temporary`) does not raise it, and
  `SESSION_ISOLATION_MISMATCH` reports `requested_language` and
  `language_applied: false` like `SESSION_MODE_MISMATCH`. Dashboard launches
  keep forwarding `language`, now together with the isolation options. (#125)

### Changed

- `savedir=temporary` and `savedir=<path>` now pass native `--savedir` and
  keep a single save location (no `game/saves` or `config.extra_savedirs`).
  With `savedir=temporary`, persistent and preferences default to empty,
  and `persistent=existing` means the selected save directory's own state,
  not the player's. `savedir=<path>` also sets `RENPY_PATH_TO_SAVES` and
  `RENPY_MULTIPERSISTENT=<path>/multipersistent`, which it creates. In 0.7.5
  `savedir=temporary` only set `RENFORGE_SAVEDIR`, and `persistent` defaulted
  to `existing`. (#125)
- The `savedir` and `persistent` schema defaults are now `auto` (they were
  `""` and `existing`); with isolation off they resolve exactly as before. (#125)
- Dashboard launch and Warp validate the isolation options: `persistent`
  and `preferences` must be `auto`, `existing` or `empty` (HTTP 400
  `launch_persistent_invalid` / `launch_preferences_invalid`, which now also
  rejects `copy` and `fixture`), `home` must be a string
  (`launch_home_invalid`), and a combination a launch would refuse is a 400
  `launch_state_mode_conflict`. (#125)
- Breaking: `persistent` no longer accepts `copy` or `fixture` (they only set
  an environment marker and copied or loaded nothing); the tool schema
  rejects them, launches return `LAUNCH_STATE_MODE_CONFLICT`, and the
  dashboard answers 400 `launch_persistent_invalid`. Use
  `renforge_saves(action="import")` to bring in real slots. `persistent=empty`
  (like `preferences=empty`) now requires `savedir=temporary`: it selects a
  fresh temporary location and never deletes files, where before it unlinked
  `persistent` in whatever save location the game used. (#125)

### Fixed

### Thanks

- Thanks to @Aaron-Fine for #125.

## [0.7.5] - 2026-11-14

### Added

- Public `renforge_validate` tool: lint a clean shadow copy with optional staged
  text, measured quotas, skipped linked directories, an explicit Ren'Py
  `version`, and the resolved `sdk_version` in the result. (#107, #118)

### Changed

- Shadow lint (validation and editor commit/undo) plans the tree before copying,
  reports quota breaches with measured totals including staged changes and the
  largest contributor, skips linked directories with a recorded reason, and
  mirrors media files as zero-byte placeholders so asset-heavy projects fit the
  default byte quota. (#106, #118)

### Fixed

- `renforge_validate` refuses to report success on reported lint errors,
  timeouts, or truncated transcripts. The lenient editor commit/undo gate still
  fails on timeouts and treats a long transcript as diagnostic; set
  `RENFORGE_SHADOW_STRICT_LINT=1` to make reported lint errors and truncation
  block that gate too. A validation exception marks the editor transaction
  failed. (#106, #107, #118)

### Thanks

- Thanks to @AxelBeary for #118.

## [0.7.4] - 2026-11-12

### Added

- `renforge_launch(language=...)` sets `RENPY_LANGUAGE` for a fresh game
  process. Results report `language_applied` (`true`/`false`, or `null` when the
  bridge does not report a language) and a `warning` when the default savedir
  lets the preference persist. (#112, #121)
- Activity log write receipts: mutating tools report `files_touched` /
  `files_deleted`, `stop` lists the paths it removed, and path lists too large
  for the journal spill to 0600 sidecars capped at the newest 200. (#104, #116)
- Live Editor: `renforge_editor(action="save", dry_run=true)` previews a
  unified source diff without writing files, reloading, or creating a
  transaction. Older bridges fail closed with `DRY_RUN_UNSUPPORTED`. (#119)
- Live Editor: optional `RENFORGE_ALLOW_PATHS` narrows saves and undo to
  game-relative POSIX glob patterns. Out-of-scope targets and invalid patterns
  are refused before a transaction is written. (#120)

### Changed

- `renforge_scene_tree(include=["overflow"])`: dropped the `clipped` alias
  (it duplicated `overflow`), added per-axis `overflow_axes` and `wrapped`
  values, and now reports `overflow` only for actual vertical clipping.
  Horizontal wrapping is reported in `overflow_axes.x` / `wrapped.x`, not as
  overflow. Verdicts also cover text inside `button` and `input` nodes, and
  `include=["style"]` now includes integer `xmaximum`/`ymaximum`.
  (#105, #108, #117)
- MCP documentation covers scripted and non-interactive tool calls through
  the FastMCP CLI, JSON arguments and results, explicit project selection,
  policy authorization, and session ownership. (#122)

### Fixed

- A failed launch no longer reports `language_applied: true` just because the
  requested language was already active in another session. (#112, #121)
- Live Editor Save keeps polling after `reload_script` resets the bridge
  connection, instead of failing the status read with "Connection reset by
  peer". (#123)
- Bridge reload poll deadline test uses a virtual clock instead of the OS
  timer, avoiding flaky failures on Windows. (#124)

### Thanks

- Thanks to @AxelBeary for #116, #117, #119, #120, #121 and #122.

## [0.7.3] - 2026-10-07

### Added

- Public MCP tool `renforge_editor` (`status` / `select` / `save`) so agents
  can drive the Live Editor without private `editor_task0_*` handlers.
  Catalog 54 → 55. (#102)
- Live Editor: select and move a literal one-line `add` or decorative `frame`
  that has a single authored `id` and integer `xpos`/`ypos`, using scene-walk
  hit-testing. Overlapping non-focusable hits stay locked (`AMBIGUOUS_HIT`).
  (#103)
- Live Editor: unlock move and Save for the standard namebox (`say.who`) via
  `gui.name_xpos` / `gui.name_ypos`, on the same style-position path as
  `say.what`. Save writes `gui.rpy` only. (#101)

### Fixed

- Keep `say.who` locked unless the speaker name is nested in the namebox
  window; a matching global `style namebox` does not unlock it. Preview
  applies position to `id "namebox"`, and the live poll survives wrapped
  bridge drops. (#101)
- Reject conditional `xpos`/`ypos` on literal `add` and decorative `frame`
  so Save cannot rewrite one branch of an expression. (#103)
- Drop nested child `add`/`frame` hit candidates regardless of scene-walk
  order. Measurement continues through position-only `Transform` wrappers
  and still stops on rotate, zoom, or crop. (#103)

## [0.7.2] - 2026-09-08

### Added

- Every pull request now runs the broad Ren'Py 8.5.3 SDK integration suite on
  Linux/Xvfb, with SDK caching and diagnostic artifacts.

### Changed

- Autopilot now identifies choices from active Ren'Py menu `items`, including
  custom-named and one-item menus, without inferring narrative intent from
  arbitrary focusable screen controls. The public `list_choices()` contract is
  unchanged.

### Fixed

- Register the live `say.what` suite in the nightly Live editor runner so the
  gate no longer fails with an unlisted suite.
- Recover task0 coordinator echo by `request_id` after the overlay periodic
  drain race so `test_editor_task0_live` no longer flakes with `coordinator
  result never applied`.

### Thanks

- Thanks to [Aaron Fine](https://github.com/Aaron-Fine) for #98: Ren'Py 8.5.3
  engine-integration CI on pull requests and Autopilot menu discovery from
  active Ren'Py `items`.

## [0.7.1] - 2026-08-17

### Added

- Operation-level runtime policy for `renforge_control`, `renforge_saves`,
  `renforge_eval`, and `renforge_run_scenario`. Default remains
  `RENFORGE_POLICY=off` (classify and record, never deny). `enforce` mode
  blocks destructive and open-world operations unless `authorize=true` or an
  allowlist permits them. See [docs/POLICY.md](docs/POLICY.md).

### Changed

- Graph inspection no longer rewrites `renpy/dump.py` inside a discovered or
  cached Ren'Py SDK. `compile --json-dump` injects an isolated `.rpe.py`
  adapter via `RENPY_SEARCHPATH` so Node-keyed `script.namemap` labels
  (Ren'Py 8.4.1+ / 8.5.x) are emitted without mutating shared or read-only
  SDK files. The adapter still normalizes Node keys when upstream `dump.py`
  already unwraps them; string-keyed namemaps stay unchanged. See
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- MCP tool registration is split into domain modules under
  `src/renforge/tool_registration/`. `server.py` only bootstraps the app;
  wrappers, contract metadata, and fail-closed registration live next to each
  other by domain. The public 54-tool MCP API is unchanged.

### Fixed

- Dismiss `say` / `nvl` / `bubble` / `choice` immediately on editor jump so a
  pending hide transition cannot raise `TypeError: missing a required
  argument: 'who'`.

## [0.7.0] - 2026-08-11

### Added

- **Live Editor (headline capability).** Launch with the in-game visual editor
  **enabled by default** (`renforge_launch`): select controls from the canvas
  or scene tree, inspect editable vs locked targets, preview layout changes at
  runtime, and **Save** source-safe edits to guarded `.rpy` files. Locked and
  unsupported targets remain inspectable with clear reasons. Public guide:
  [docs/LIVE_EDITOR.md](docs/LIVE_EDITOR.md). `renforge_info` /
  `renforge_context` advertise a structured `live_editor` capability
  (default-on, launch tool, agent workflow) so agents discover the feature
  without private `editor_task0_*` handlers.
- **Scene perception suite for non-multimodal agents.** `renforge_scene_tree`
  reports the whole frame as structured, logical-coordinate nodes — every layer
  displayable, focusable control, and (unlike `renforge_list_ui_elements`)
  non-focusable text — each with `id`, `type`, `bounds`, `center`, `zorder`,
  and `screen`, plus an `omitted` completeness hint so an agent can widen its
  view precisely. `detail` (`semantic`/`layout`/`raw`), `layers`, `types`,
  `screen`, and `ids` scope the result; `include=["color","style","overflow"]`
  adds composited pixel colour, declared style, and best-effort text overflow;
  `format="wireframe"` renders an ASCII map; `save_as`/`diff_against` persist
  and structurally diff scene snapshots under `<project>/.renforge/scenes/`.
- `renforge_measure` computes pixel relationships between scene nodes (by `id`)
  or literal bounds without vision: `align`, `gap`, `distribute`, `center`,
  `overlap`, `fit`, and WCAG `contrast`. Returns logical-pixel deltas and, when
  a `tolerance` is given, a `pass` verdict.
- Complete Simplified Chinese (`zh-CN`) coverage for the dashboard, including
  localized runtime status messages, project-browser roots, and Story Map node
  types.

### Changed

- Dashboard bundles are now generated once by CI and release workflows, remain
  untracked in Git, and are validated inside both wheels and source archives.
  PyPI and `uvx` users continue to receive a ready-to-run dashboard without
  needing Node.js.

### Fixed

- Conflict-preserving source publication: commits and rollbacks exchange source
  files with an atomic CAS primitive, retain original/staged/displaced evidence
  on conflict, and never fall back to unconditional overwrite.
- Private bridge and artifact lifecycle: schema-3 ownership manifests, authenticated
  quit, deferred teardown with `SHUTDOWN_INCOMPLETE`, and dashboard/direct launch
  as a single lifecycle owner.
- Live editor interactions: status codes with localized HUD text, clear-selection,
  intent-based unsaved counts, effective inspector properties, structural z-order
  mutex, responsive layout metrics, tree truncation, and shipped editor assets.
- Bridge readiness is now published only after the listener accepts connections,
  eliminating a startup race that could surface as intermittent connection refusals.
- `renforge_launch` keeps the Live Editor enabled by default while honoring an
  explicit `editor=false` opt-out across dashboard and direct launch paths.
- Live Editor tree selection now highlights only the exact runtime representation;
  live-proof runners preserve structured reload codes and target focusable button
  padding when validating locks, eliminating false failures in the 21-suite gate.
- The working-tree Live Editor launcher now reads and atomically restamps schema-3
  artifact manifests instead of the removed editor-session manifest.
- Windows TEMP dashboard discovery now uses a process-stable user digest, and
  Windows source CAS fails closed if `ReplaceFileW` is unavailable.
- The `test` extra now installs `httpx2>=2.0.0`, the client backend required by
  current Starlette `TestClient`, instead of falling back to deprecated `httpx`
  compatibility and emitting `StarletteDeprecationWarning`.
- Missing dashboard assets now return a stable, actionable HTTP 503 response
  instead of a generic 404.

### Thanks

- Thanks to [@AxelBeary](https://github.com/AxelBeary) for the complete
  Simplified Chinese dashboard translation and the related i18n fixes.

## [0.6.6] - 2026-07-24

### Fixed

- Pointer operations now use one shared bridge input layer. Native hover prefers
  `renpy.set_mouse_pos(..., duration=0)`, clicks synchronously deliver balanced
  test-marked mouse-button events to the focused displayable, and fallbacks always
  release `testmouse` state so physical input immediately reclaims hover control.
- Runtime discovery now prefers validated project-local Ren'Py SDKs and safely
  falls back to compatible managed SDKs with locked, atomic cache repair.
- Concurrent RenForge servers now isolate bridge ownership per project,
  preserve dashboard-owned sessions, and retain locks through incomplete
  teardown so one launch cannot overwrite another session's artifacts.

## [0.6.5] - 2026-07-21

### Fixed

- Long Windows startups no longer outlive the MCP request budget.
  `renforge_launch` returns `status="starting"` after at most 20 seconds while
  startup continues in the background; `renforge_launch_status` reports the
  final state, `renforge_stop` cancels an in-progress launch cleanly, and a
  competing launch returns `LAUNCH_IN_PROGRESS` instead of dropping its parameters.

## [0.6.4] - 2026-07-20

### Fixed

- `launch_with_bridge` no longer returns prematurely when `ping()` returns
  `{"error": "timeout_waiting_for_main_thread"}`. On Windows or during heavy asset
  loading / script compilation, Ren'Py can take 30–40s before `periodic_callbacks`
  starts draining on the main thread. Previously `client.ping()` returned the dict without
  raising, causing `launcher.py` to declare the session `ready` prematurely while the
  game was still in `init python`. Subsequent MCP tool calls then timed out or failed.
  `launch_with_bridge` now strictly verifies `reply.get("pong") is True` and retries
  until Ren'Py reaches its main interaction loop, with default `startup_timeout`
  increased from 60s to 90s.

## [0.6.3] - 2026-07-20

### Fixed

- Bridge listener no longer dies with `NameError: name 'socket' is not
  defined` after `renpy.reload_script()`. The listener thread survives a
  script reload, but the reload wipes the Ren'Py store — the `__globals__`
  of `init python:` functions — so the bare `except socket.timeout:` in the
  accept loop raised `NameError` on the next 0.5s timeout and silently
  killed the thread. The game kept running with a dead bridge until the
  process was eventually closed. The listener and its helpers now use
  function-local imports (read from `sys.modules`, which reload never
  touches), and the accept loop also tolerates non-timeout `OSError`.

## [0.6.2] - 2026-07-19

### Fixed

- Ren'Py **8.5** support: read button/choice labels via `_tts_all(raw=False)`
  (8.5 made `raw` required; the old no-arg call failed open and blanked every
  control label, breaking choice selection and autopilot).
- Repair Ren'Py 8.5 `--json-dump` label emission when discovering/installing an
  SDK: unwrap Node-keyed `script.namemap` entries that upstream `dump.py` still
  filters with `isinstance(name, str)`.

### Changed

- Default pinned Ren'Py SDK is now **8.5.3** (was 8.3.7).

## [0.6.1] - 2026-07-19

### Changed

- Public-launch polish: install-first README with light/dark dashboard
  screenshots, architecture and client-config details moved to `docs/`,
  English-only dashboard labels.
- Published to the official MCP Registry (`io.github.alex-jordan547/renforge`):
  `server.json` manifest and PyPI ownership marker.

## [0.6.0] - 2026-07-16

### Added

- Compact live-state profiles (`minimal` / `interaction` / `debug` / `full`) with
  serialization limits on `renforge_wait_until` and `renforge_game_state_compact`.
- `renforge_launch` strategies: `display=auto`, `audio=auto`, structured launch
  errors (`code` / `phase` / `suggested_fix`), and `savedir=temporary` isolation.
- `renforge_hit_test` for interactive focus-stack inspection; UI elements now
  report `action`, `zorder`, `covered`, `clickable`, and logical coordinates.
- `renforge_run_scenario` to batch set/click/wait/assert steps with automatic
  failure diagnostics.
- Structured business events (`quick_save.completed`, `quick_load.completed`,
  `skip.started`/`skip.stopped`, `auto.changed`/`auto.advanced`,
  `rollback.completed`) with `correlation_id` / `interaction_id`.
- `wait_for_effect` on `renforge_control` and `renforge_click_element` to block
  until the matching business event is observed.

### Changed

- `renforge_wait_until` returns a compact interaction-profile state by default
  and a structured `matched` object (`type` + `value`).
- Clicks report the element that actually received the event (`received_by`)
  when another control covers the target.
- Bridge `poll_events` entries include `timestamp` and optional
  `correlation_id` for attribution.

### Fixed

- Translation estimation discards pixels shifted outside the search region
  instead of wrapping them around the opposite edge.
- Live save/load verification now enforces the documented `restored_label`
  response contract.

## [0.5.0] - 2026-07-13

### Added

- Added `renforge_hover_element` to move the pointer over visible controls
  without clicking, with stale-frame protection.
- Added `renforge_capture_screenshot` for reusable named PNG captures and
  `renforge_estimate_translation` for measuring visual movement between them.
- Added `renforge_get_ui_element_bounds` to report focus bounds and the
  alpha-painted bounds of the active `ImageButton` state.

### Changed

- ImageButton hover now dispatches Ren'Py's focus mouse handler before
  restarting the interaction, so hover state updates reliably without a player
  interaction loop.

## [0.4.1] - 2026-07-13

### Added

- `renforge_info`/`renforge_context` now resolve `active_project` without the
  dashboard: they fall back to the `serve --project` default, then to a Ren'Py
  project auto-detected from the current directory, and report the winning
  source in a new `project_source` field. When nothing matches, the payload
  carries an explicit `hint` so agents pass `project_path` directly instead of
  stalling.

### Changed

- `renforge_screenshot` accepts a single `width` or `height` and derives the
  other dimension from the game's aspect ratio (previously both were required
  together).

## [0.4.0] - 2026-07-12

### Added

- Added grouped runtime controls through `renforge_control`, including hot
  script reload, rollback, quicksave/quickload, skip, auto-forward, and menu
  actions.
- Added named save slots with `renforge_saves` and grouped text, key, and scroll
  input with `renforge_send_input`.
- Added `renforge_get_errors` for recent bridge exceptions and bounded crash-file
  diagnostics, plus `renforge_wait_until` for bounded label, screen, or expression
  waits.
- Added `renforge_inspect_screen` for active screen scope and argument
  introspection.

### Changed

- Extended `renforge_game_state` with opt-in `include=["metrics", "audio"]`
  sections while preserving its default response shape.
- Expanded the README and MCP reference with the complete edit, hot-reload,
  save, branch, and diagnostics workflow.

## [0.3.0] - 2026-07-10

- Added pixel-perfect screenshot, displayable measurement, live positioning,
  and screenshot-diff tools.
