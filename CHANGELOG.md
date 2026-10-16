# Changelog

All notable RenForge releases are recorded here. Versions follow semantic
versioning.

## Unreleased

### Changed

- `renforge_saves(action="import")` copies only into a temporary session's own
  savedir. A session launched with `savedir=<path>` is refused with
  `SAVE_IMPORT_NOT_ISOLATED`: launching there and importing are each
  mutating, so under `RENFORGE_POLICY=enforce` the two together copied the
  player's saves into any folder without `authorize=true`.
- `list_user` and `import` read the player's saves only from one folder of
  the Ren'Py save root named by the game's `config.save_directory`; a name
  like `../OtherGame` is ignored, as is a `game/saves` that links out of the
  project.
- Under `RENFORGE_POLICY=enforce`, reading the player's own saves needs
  `authorize=true`: `renforge_saves` `list_user` and `import` and
  `renforge_session` `import` are destructive, and `renforge_session list`
  leaves out `player_slots` (with `player_slots_hidden`) unless authorized.
  A launch whose `savedir` or `home` is a path outside the RenForge state
  folder is destructive too (the shared temp dir is not RenForge's), since
  the game writes its saves, persistent data, and config there. The policy
  reads the path the launch uses, stripped and expanded.
- A launch reuses a running game only when it matches the request:
  `SESSION_ISOLATION_MISMATCH` when the game can reach the player's saves
  but the launch asked for an isolated session (it was handed back without
  the authorization `savedir=existing` needs), and `SESSION_EVAL_MISMATCH`
  when a play-only server would get a bridge that runs code. A game this
  server did not start answers the new bridge command `session_info`; one
  that cannot is assumed to match neither. Launches through the dashboard
  pass the calling server's `bridge_eval`, so a play-only server's game
  refuses eval even when the dashboard's own toolsets allow it.
- Tools that need a running game say so when none is: `code="NO_RUNNING_GAME"`
  with a pointer to `renforge_session(action="launch")`, or `GAME_STARTING`
  while it starts. Both were reported as "bridge metadata failed validation".
- `renforge_act` on an id the screen does not list adds a `message` and the
  current observation to `error="missing"`.
- `renforge_info` lists the server's `toolsets` and, when `play` is listed,
  the play loop's steps; it leaves out the Live Editor block when the `editor`
  toolset is not listed. The server instructions likewise name only the
  listed tools, and say what each `forward` value asks of the agent.
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
- Launches in a Wayland session use `SDL_VIDEODRIVER=wayland,x11` so games
  start while the screen is locked. The startup deadline defaults to 180 s and
  reads `RENFORGE_STARTUP_TIMEOUT`.
- Sessions with isolated preferences start with transitions off
  (`RENFORGE_TRANSITIONS=on` keeps them). `observe`, `act`, and
  `advance_until` return a compact observation by default (`detail=full` for
  every field), and `act` accepts `observation_id` alone as its guard.
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
  (`size`, `truncated`) instead of silently dropping entries. `explain(source=...)` on a menu, `if`, or
  `while` lists what each branch does, and label and screen entries name
  their file only when it differs from the result's own.
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
  `started_at_label` because earlier story state never ran.
- Play sessions keep their saves across launches in the project's RenForge
  state folder. Continuing skips through pauses, clicks past a line that only
  has optional buttons beside it, ignores HUD buttons on `show screen` screens
  during clickable lines, and stops with `stop="loop"` on a line it already
  passed; a time-budget stop is no longer reported as a failure.
- `RENFORGE_TOOLSETS` lists only chosen tool groups (`play`, `live`, `debug`,
  `editor`, `project`, `translation`, `build`, `docs`); it replaces the
  fork-only `RENFORGE_TOOLS` filter.
- Lint, compile dumps, translation runs, and shadow lint run Ren'Py with a
  disposable HOME and one disposable save location. Ren'Py writes a game's
  persistent data on every start, so these used to rewrite the player's
  `~/.renpy/<game>/persistent` and `game/saves/persistent`.
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
  scene that decodes large images) no longer ends `act` or `advance_until`
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
- Engine-based ending evidence in gameplay observations and progression:
  `stop="end"` confirms a root script return at the main menu; `main_menu`
  distinguishes initial menus and explicit restarts. Evidence persists until a
  new run, and confirmed endings emit a cursor-readable `ending` event.
- Every pull request now runs the broad Ren'Py 8.5.3 SDK integration suite on
  Linux/Xvfb, with SDK caching and an explicit diagnostic artifact allowlist.

### Changed

- Simplified shared discovery: owning screen instances supply named widget IDs,
  and the collector assigns IDs/facts once for observation and actions. Removed
  obsolete discovery helpers and scene placement calculations; both host
  progression modes now live in `progression.py`, with compatibility exports.
- Updated gameplay/tool guidance for clipped menu entries, incomplete discovery,
  content-based frame guards, process-exit status, and unavailable text overflow.
  Historical harness proposals are distinguished from current TODOs.
- Unified gameplay and legacy UI discovery around one control collector and
  canonical IDs. Engine menu actions expose clipped choices on custom layers;
  ambiguous captions are refused and selections accept observation guards.
- Gameplay images preserve source frame identity and return a frame descriptor
  for guarded clicks on resized images, independently of the delivered PNG hash.
- Scene geometry uses completed renders and composes transforms/clipping.
  Repeated placements retain render paths; alpha inspection uses cached renders.
  Bounded focus probes find usable points on partially covered/masked controls.
- Unsupported custom event handlers are explicit input candidates and stop both
  progression modes. Conditional custom eligibility remains unknown.
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
- Default progression uses bounded native skip, with interaction-level guards,
  time/step leases, temporary unread-text and presentation preferences, and
  guaranteed restoration while the engine is responsive. `mode="dismiss"`
  preserves guarded dismissal; both modes stop before actionable UI.
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
- `renforge_act` returns the next stable observation by default, with optional
  `advance=true` progression, `submit=false` input editing, and session and
  observation guards. Action success remains separate from follow-up status;
  `observe=false` preserves the previous action-only response.
- Custom overlay hubs remain gameplay controls. Held keys can be released after
  interaction changes; viewport axes are validated before mutation; drag
  capabilities and target eligibility are checked before callbacks run.
- Crossed dialogue preserves repeated occurrences and reports event-buffer gaps;
  rendered text replaces older-engine callbacks containing source interpolation.


- Autopilot now identifies choices from active Ren'Py menu `items`, including
  custom-named and one-item menus, without inferring narrative intent from
  arbitrary focusable screen controls.
- `renforge_launch`, `renforge_jump`, `renforge_new_game`, and dashboard
  launches now isolate the running game from the user's Ren'Py state by
  default. Each session gets a disposable save directory (`savedir=temporary`,
  native `--savedir` plus `RENFORGE_SAVEDIR` / `RENPY_PATH_TO_SAVES` /
  `RENPY_MULTIPERSISTENT`), a private HOME/XDG/temp tree, empty persistent
  data, and reset preferences. Ready payloads include `session_id` and an
  `isolation` object so an agent can see the mode. Pass `savedir=existing`
  (which also keeps host HOME unless `home` is set) or set
  `RENFORGE_ISOLATION=existing` to use the user's files; that is classified
  destructive and needs `authorize=true` when `RENFORGE_POLICY=enforce`.
  `renforge_saves` gains `list_user` (observational host listing) and `import`
  (copy selected host slots into the isolated session; never bind-mounts the
  user tree). This is application-layer isolation, not the Linux
  Bubblewrap/FUSE contract suite, which remains test-only and is not on the
  MCP launch path.

### Fixed

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
