# Changelog

All notable RenForge releases are recorded here. Versions follow semantic
versioning.

## Unreleased

### Changed

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
- Bounded skipping is about 7x faster on a standard GUI (138 to 19 ms per
  line; Stranded in Space 185 to 15 ms). Launched games run without vsync
  (`RENFORGE_VSYNC=on` keeps it), a game's `config.skip_delay` is set to 0
  during an operation and restored after, each line is checked once, the
  host polls a cheap operation status instead of full observations, and the
  geometry walk avoids Ren'Py's rollback-tracked containers.

### Added

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
