# RenForge Runtime Test Harness TODO

## Current remaining work

- Improve useful geometry/eligibility reporting for arbitrary custom input where
  supported engine evidence exists; conditions, masks, and prerequisites remain
  unknown today. Keep screenshot/descriptor input as the generic fallback.
- Broaden real-game ending coverage and engine/platform checks. Custom screens,
  restarts, and clean exits alone do not prove an ending.
- Add immediate MCP disconnect cancellation once a tested backend hook exists;
  preserve the bounded engine lease meanwhile.
- Settings text polish remains deferred. A full Academy34 introductory-week
  playthrough remains unfinished and requires a renewed request.

The completed-work records below preserve validation history. The final nine-step
section is a historical harness proposal, not the current implementation plan.
Its unchecked items have broader acceptance criteria; existing observe/action/
progression tools already implement parts of that proposal.

## Agent play loop simplification (2026-10-12)

Goal: the five-tool loop the playthroughs actually used (`session`, `observe`,
`act`, `explain`, `mod`), with Ren'Py supplying the facts instead of the bridge.

### Cuts

- [x] Fold `launch`, `launch_status`, `saves` (save/load/list) and `stop` into
  one `renforge_session` tool with an `action` argument.
- [x] `act`: replace `text`, `value`, `x`, `y`, `drop` with one `value`
  interpreted by the control's type (typed text for an input, a number for a
  bar, an x/y pair for a viewport, a target control id for a drag).
- [x] `act`: physical keys become pseudo-controls (`key/escape`, `key/tab`);
  `hold` becomes that control's value. Drop `key`, `hold`, and `submit`
  (inputs always submit; editing without submitting moves to `value` options
  only if a game needs it).
- [x] Guards: `observation_id` only. Drop `interaction` and `session_id` from
  `act` and the guarded legacy calls.
- [x] Progression: `act` advances to the next decision by default within one
  time budget. Retire `advance_until` (keep it as an alias for one release),
  `mode=dismiss`, `max_steps`, and the `observe` flag.
- [x] Launch: agent-mode defaults only. Remove `savedir`, `home`,
  `persistent`, `preferences`, `cleanup_on_stop`, `display`, `audio`, and
  `timeout` from the agent-facing tool; keep them as server configuration
  (environment variables) for the rare user who needs them.
- [x] Compact observations only. Remove `detail`; `explain(id)` returns a
  control's full facts on request.
- [x] Group upstream's editor, translation, build/distribute, and docs tools
  into separate toolsets the server enables on request, replacing the
  fork-only `RENFORGE_TOOLS` filter.

Done 2026-10-12: the `play` toolset is the canonical loop. Earlier tools stay
in the `live`, `debug`, `editor`, `project`, `translation`, `build`, and `docs`
toolsets rather than being removed; `advance_until` (fork-only) was retired.

### Related findings

- [ ] Test overlap in upstream-owned files (left alone so merges stay clean;
  raise with upstream): tool-definition substring and schema checks already
  pinned by the contract snapshot (`test_tool_definitions.py`); the vbar/
  slider/bar editor lock matrices (`test_editor_source.py`) and the
  say-who/say-what mirror files; per-widget `test_editor_*_live.py` files that
  share one template; demo live tests that could share launches (screen
  introspection, scene-tree ids, hover/modal fixtures, editor overlay).

- [ ] One id scheme. `list_ui_elements` and the tools built on it keep
  upstream's ids while `observe` uses `screen/...` ids; `control_id` links
  them. Settle on one when upstream agrees.
- [ ] Perception tools that are views of the same collector as `observe`:
  `screenshot`, `scene_tree`, `list_ui_elements`, `get_ui_element_bounds`,
  `get_displayable_bounds`, `hit_test`, `inspect_screen`, `list_choices`,
  `game_state`, `game_state_compact`. Action tools that duplicate `act`:
  `advance`, `click_element`, `click_at`, `hover_element`, `select_choice`,
  `send_input`. Move them to a compatibility toolset.
- [x] Engine facts instead of recomputation (2026-10-12): modal blocking
  follows `Render.take_focuses` (a modal render hides what was drawn before
  it; a window modal only inside its rectangle) instead of ranking screens;
  "not drawn yet" reads `interface.interact_time`; roles come from Ren'Py's
  widget classes, names only for non-engine displayables. Kept: transition
  state already reads the engine's `ongoing_transition` and instantiated
  delays; text-tag stripping is one shared implementation used by host and
  bridge (not a duplicate); render-tree geometry for non-focusable content
  has no engine equivalent.

## Simplification and documentation review (2026-10-04)

- [x] Remove unused legacy ID/action helpers and obsolete scene placement/
  animation-time calculations. Scene nodes consume completed-render geometry
  directly rather than converting bounds twice.
- [x] Assign widget IDs/facts in the shared collector once, and reuse its records
  for actions and drag targets. Named IDs use the actual owning screen instance;
  discovery does not resolve screen names on a guessed default layer.
- [x] Keep both host progression modes and settling/failure handling in
  `progression.py`; preserve existing `renforge.observe` imports as compatibility
  exports. Remove redundant classification branches and stop normalization.
- [x] Synchronize README, MCP guide/tool catalogue, architecture, cloud setup,
  changelog, tool descriptions, and public contract snapshot. Correct visible-only
  choice/control claims, image-vs-interaction identity, capture guard semantics,
  process-exit statuses, and unavailable natural-text overflow. Label the old
  harness proposal separately from current remaining work.

Full regression validation: **1,136 passed, 70 skipped**, with five existing
FastMCP deprecation warnings. Compilation and diff checks passed.

Live validation: **8 checks passed on Ren'Py 8.5.3** and **4 checks on 8.0.3**
(Linux, existing display). Commands:

```bash
RENFORGE_SDK_TESTS=1 RENFORGE_SDK_VERSION=8.5.3 .venv/bin/pytest -q tests/test_integration_sdk.py -k 'scene_tree or custom_layer_scaled or act_edits_input or bounded_progression_restores or demo_control_supports_editor_save'
RENFORGE_SDK_TESTS=1 RENFORGE_SDK_VERSION=8.0.3 .venv/bin/pytest -q tests/test_integration_sdk.py -k 'custom_layer_scaled or act_edits_input or bounded_progression_restores'
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src tests
git diff --check
```

## Active work: bounded native skip and playthrough fixes (2026-10-03)

The Divine Intervention MCP playthrough exposed these generic runtime issues.
This section tracks the current implementation; the historical roadmap below
remains separate and its full acceptance criteria are not implied complete.

- [x] Make bounded native skip the default for `advance_until` and composed
  `act(advance=true)`. Stop before choices, text inputs, custom actionable UI,
  and unknown interactions; enforce time/step limits inside the bridge.
- [x] Temporarily allow unseen narrative while progressing, restore preferences,
  and disable owned skip on completion, errors, or an expired operation lease.
  Keep guarded dismiss progression as an explicit compatibility mode.
- [x] Add optional screenshots to standalone progression and bound gameplay PNG
  responses so ordinary MCP clients can receive them. Hash the returned image
  and expose scaling metadata without changing logical control coordinates.
- [x] Distinguish frame timeouts, transport failure, and verified game crashes.
  Preserve successful actions; mark lost action replies as uncertain rather
  than inviting unsafe retries.
- [x] Reserve time for cleanup, settled final observation, and transcript
  retrieval; poll events during progression to avoid buffer gaps.
- [x] Use rendered/interpolated speaker names and dialogue in crossed events.
- [x] Return fresh observations with stale-action refusals, and avoid advertising
  a departing say interaction as stable at the end of a progression batch.
- [x] Cover boundaries, cleanup, ambiguous outcomes, image limits, and repeated
  dialogue with regression tests; verify default skip on live Ren'Py fixtures.
- [x] Update MCP descriptions, public contract, architecture, changelog, and
  playthrough findings with the implemented behavior and remaining limitations.


Validation: the Python suite passed **1,096 tests** (61 opt-in/environment
skips). The broad Ren’Py **8.5.3** live suite passed **28 checks**; the four
progression fixtures also passed on **8.0.3**. After the final delayed-start
expiry fix, the four progression fixtures passed again on both versions.
Compilation, public tool contract, dependency compatibility, and diff checks
passed. A manual MCP-only replay of Divine Intervention 0.1.2 reached the
available build's ending and returned naturally to the main menu: nine bounded
progression calls, 769 native advances, no truncated transcripts or failed
responses. See `docs/evidence/divine-intervention-playthrough.md`.

Ending-detection follow-up validation: **1,111 Python tests passed** (65
opt-in/environment skips), plus four ending fixtures on each of Ren'Py **8.5.3**
and **8.0.3**. A further complete manual MCP-only Divine Intervention route
returned `stop=end`, confirmed exactly one ending event, and preserved ending
evidence while entering and leaving Settings. Compilation and diff checks passed.

Remaining work related to this change:

- [x] Require Ren'Py 8.0 or newer for runtime control. Reject legacy bundled
  engines and selected SDKs with a structured incompatibility before download,
  injection, or launch, including explicit SDK overrides. Verified through MCP
  against Escape From the Hospital (bundled engine 7.0.0); stop rather than
  attempting a legacy-engine backport or migration, per the user's policy.
  Validation: 1,118 Python tests passed (65 skips), and five supported 8.5.3
  live isolation/ending checks passed. Documentation and MCP contract updated.

- [ ] Integrate an immediate MCP cancellation/disconnect hook when one can be
  tested across supported backends. The in-engine lease currently bounds cleanup;
  a blocked engine main thread cannot restore state until it resumes.
- [x] Repeat the manual MCP-only Divine Intervention playthrough through the
  available build's ending, without scripts or game-specific workarounds.
- [ ] Broaden real-game ending coverage and the engine/platform matrix. One
  completed route and synthetic fixtures do not prove general compatibility.
- [x] Expose generic main-menu/end boundary information in progression results.
  Observations expose engine `lifecycle` evidence. Both progression modes return
  `stop=end` only for a root script return confirmed at the engine main menu;
  initial menus and explicit restarts return `main_menu`. Evidence persists
  through menu observations/Settings and clears on start/load/warp/replay.
  Called scenes, nested contexts, init, and replay returns cannot establish an
  ending; failed final observation cannot establish current completion.
  Verified on live Ren'Py 8.5.3/8.0.3 fixtures and a second MCP-only complete
  Divine Intervention route (`game/script.rpy:2353`, ending id 1).
- [ ] Broaden ending evidence for games that stay in custom end screens, exit,
  or explicitly restart. Current engine-run detection intentionally requires
  a root `return` followed by the main menu; those alternatives remain
  unconfirmed rather than being inferred from game-specific labels or text.
- [ ] Improve control text normalization for nested/accessible Settings widgets.
  The replay exposed repeated labels such as `RETURN: RETURN: RETURN` and
  unnamed bars as `Barbar`; observed IDs still supported all required actions.
  Deferred at the user's request: entering and leaving Settings is sufficient
  for now, and both actions passed the MCP-only ending-detection replay.

## Academy34 free-roam follow-up (2026-10-04)

- [x] Fix coordinate input for unfocused custom displayables that calculate
  pixel-mask hover during rendering. Keep the synthetic cursor alive across
  motion/render/click, defer the reply until cleanup, serialize pending input,
  and bound the operation with a lease and interaction-generation guard.
  Validation: 170 bridge tests; the real render-hover fixture on Ren'Py 8.5.3
  and 8.0.3; 1,122 full-suite passes (66 skips). Academy34 receptionist,
  navigation, and story scenes worked through public MCP clicks after the fix.
- [x] Discover or explicitly flag unsupported custom input displayables on
  arbitrary layers. Academy34's parking/room hotspots currently produce empty
  controls and `omitted.unclassified=false`, despite visible actionable content.
  Implemented: custom handlers produce `input_candidates` and incomplete-input
  metadata, with completed-render geometry. No game class/action/label matching.
- [x] Expose transformed/viewport choice buttons consistently. Academy34's
  ordinary choice screen exposes its viewport but omits the choice buttons from
  `observe` and `list_choices`; now recovered through active engine menu actions
  on arbitrary layers, including clipped entries and duplicate captions.
- [x] Compose scene-tree transforms into reliable logical bounds. The game's
  choice tree reported design-space 1920x1080 bounds in a 1066x600 runtime;
  observation and scene inspection now share transformed/clipped render bounds.
- [x] Unify coordinate/hash semantics between bounded gameplay screenshots and
  low-level screenshot guards/clicks. `observe` may return a resized image,
  whereas `click_at(coordinate_space="screenshot")` uses the engine capture.
  Implemented: pass the returned `frame` descriptor for screenshot-pixel clicks.
  It preserves source identity and performs the conversion/guards in the bridge.
- [x] Document the partial Academy34 live test, its input workaround, generic
  fix, remaining gaps, checkpoint, and cleanup in
  `docs/evidence/academy34-playtest.md`. Stopped at the user's request at Sunday
  midnight, at the Sleep/Cancel choice, after the second Sonya scene.
- [ ] Complete the introductory-week playthrough if requested again. It is
  unfinished, and this run established no engine ending. Keep route completion
  evidence separate from a confirmed root script return at the main menu.
  Resume isolated slot `mcp-sunday-midnight` in `/tmp/academy-resume-saves`.
- [ ] Improve inspection of conditional hotspots and spatial navigation.
  Background signs and unavailable targets currently look actionable while
  ordinary discovery exposes no target geometry or eligibility. Read-only
  runtime inspection was needed to find the active upper-floor door and explain
  a vague sleep gate. Rendered candidate geometry and explicit uncertainty now
  improve inspection without eval; labels, masks, conditional eligibility, and
  story prerequisites of arbitrary custom handlers remain unknown. Some Academy34
  region renders span the entire frame, so their bounds cannot identify a door.

### Implemented generic interface fixes

Prefer one rendered scene inventory, one control collector, and one frame
contract. Keep the existing tools as views/actions over those shared helpers.
Do not recognize game class names, private `click_action` attributes, story
flags, route labels, or screen names. This plan covers the observed input and
inspection gaps; it does not add a route solver or infer custom endings.

#### 1. Make frame identity and screenshot coordinates consistent

- [x] Preserve the bridge's source capture identity instead of dropping it in
  `apply_screenshot`. Keep `frame_hash` as the hash of the exact returned image
  for compatibility; expose a separate source `frame_id` for engine guards.
- [x] Return one frame descriptor with source ID, session identity, returned
  pixel dimensions, source capture dimensions, and logical screen dimensions.
  Build it from the same capture/observation; do not recapture independently to
  describe the image. Use the same descriptor in gameplay screenshot-producing tools
  (`observe`, `act`, and `advance_until`); legacy capture/crop tools retain their
  documented image formats.
- [x] Extend the existing frame guard argument to accept this descriptor, while
  retaining existing string IDs. Route it through one normalization helper in
  the host/client/bridge. Avoid new tool names and additional guard aliases.
- [x] For descriptor-backed screenshot clicks, convert returned-image pixels
  directly into logical coordinates, then guard against the source frame/session
  and current logical dimensions. Validate finite numbers, dimensions, and image
  bounds before posting any input. Logical clicks retain their current meaning.
- [x] Keep bare legacy screenshot-coordinate calls explicitly defined as source
  capture pixels; document the descriptor path for bounded observation images.
  Do not silently guess which earlier screenshot a client meant.

Implementation: `observe.py`, `util/images.py`, `interaction.py`, bridge
screenshot/guard/coordinate helpers, `BridgeClient`, and the existing MCP
registration/schema. This requires a small backward-compatible contract change,
not a session cache, a new transport, or moving Pillow into the game SDK.

Acceptance: a pixel selected on a resized image hits the same target as the
corresponding logical point. A valid descriptor passes its source-frame guard;
a stale frame, different session, invalid dimensions, or an out-of-image point
posts no input. Returned-image hash checks remain valid.

#### 2. Unify control discovery and recover ordinary menu choices

- [x] Replace the duplicate focus enumeration in `_renforge_focusable_elements`
  and `_renforge_observe_focus_pairs` with one bounded collector. Merge current
  focus records, known engine widgets in the scene inventory, and active engine
  menu entries by identity. Assign IDs once and retain live references for action
  guards. Reuse the collector for observe, list_ui_elements, list_choices,
  select_choice, hit_test, and scene-tree control records.
- [x] Do not discard a known actionable control solely because its focus record
  lacks a rectangle. Keep bounds unavailable until geometry can establish them.
  Use engine sensitivity and the active screen/menu context to decide whether
  its semantic operation is available.
- [x] Read menu entries from actual active screen instances on every runtime
  layer, rather than `get_screen(name)` on a default layer or a screen-name
  convention. Entries with no action are captions, not choices. Read displayed
  captions and engine-provided actions; preserve ordering and duplicate captions.
- [x] Associate rendered buttons with menu actions by identity where possible.
  Select through the engine-provided current action in its owning screen context,
  with interaction/session/observation and sensitivity guards. Do not guess a
  choice from text equality or manufacture label jumps/return values.
- [x] Separate action sensitivity from geometric visibility/coverage. Report
  clipped/offscreen choices honestly; a valid current menu action can support
  semantic selection independently of a viewport's focus rectangle. Unrendered
  ordinary screen buttons must not acquire eligibility merely from being found
  in a named-widget dictionary. Preserve documented legacy index behavior or
  explicitly migrate it with regression coverage; never silently remap indices.

Implementation: the existing scene inventory and shared `interaction.py`
contract, plus the bridge's menu/action wrappers. Reuse the existing generic
engine-widget adapters; do not introduce a plugin/adaptor framework for this fix.

Acceptance: scaled, nested, scrollable menus on custom layers expose their
selectable entries and can select an eligible entry through ordinary MCP tools.
Duplicate captions remain distinct; captions and disabled actions cannot be
selected; stale selection executes nothing. Observe and list/action tools agree
on the underlying controls.

#### 3. Use completed render geometry everywhere

- [x] First verify the completed-render introspection available in Ren'Py 8.0.3
  and 8.5.3 with a small fixture. Put any version differences behind one internal
  helper. If an engine cannot establish geometry, return unavailable bounds
  with a reason; do not reconstruct a second speculative layout engine.
- [x] Join displayable identity from the shared inventory to its completed render
  occurrences. Compose render offsets/transforms and viewport clipping into
  logical screen coordinates. Keep repeated placements distinguishable rather
  than dropping all but the first occurrence of a widget.
- [x] Prefer engine focus rectangles for focused controls; use the render-backed
  geometry for the other supported widgets and scene nodes. Remove isolated
  `render_for_size(..., screen_width, screen_height, 0, 0)` calls and offset-only
  bounds reconstruction from inspection. Inspection must not re-render game
  widgets or execute their input/actions to discover geometry.
- [x] Share the result with observe, scene_tree, widget bounds/alpha inspection,
  and hit_test. Image-tag bounds retain the supported engine placement API.
  An axis-aligned bound describes an enclosing region, not a guaranteed hit.
  Use engine focus/mask checks for supported point eligibility. Do not declare
  a whole control covered simply because its center is transparent or occluded;
  retain uncertainty where no eligible point can be proven.

Acceptance: nested zoom/rotation, viewport scrolling/clipping, custom layers,
transparent focus masks, partial occlusion, and repeated placements report
consistent geometry. Clicking a proven visible point changes the expected live
fixture state. Hidden/zero-size render occurrences do not become active targets.
Inspection alone changes no fixture state.

#### 4. Make custom-input uncertainty useful and explicit

- [x] From the rendered inventory, identify unhandled displayables with their own
  input handlers, excluding widgets already handled by engine adapters. Compare
  handler implementations with inherited engine behavior; do not identify them
  by creator-chosen class names or arbitrary action-like attribute names.
- [x] Report bounded candidate regions with screen/layer, trustworthy geometry
  where available, and an explicit unsupported/unknown reason. Set
  `omitted.unclassified` when relevant input could not be classified. A candidate
  is not a certified button: do not invent text, actions, sensitivity, masks, or
  `clickable=true`. Reuse observation/scene inspection instead of adding a tool.
- [x] Resolve only facts the engine establishes. Zero-size, clipped, hidden,
  or modal-blocked occurrences must be distinguishable from visible candidates;
  custom event conditions and story prerequisites remain unknown unless the
  game's visible UI states them. Do not inspect game flags/code automatically
  to explain a quest gate or promise to deduce every custom input handler.
- [x] Feed uncertainty into the existing progression boundary policy. Stop at
  unresolved actionable interactions; do not treat an empty controls list as
  completeness. Test persistent overlays and decorative/custom delegates so
  broad type heuristics do not unnecessarily stop every ordinary dialogue.
  Retain bounded coordinate input as the generic manual fallback.

Acceptance: an unfocused render-hover fixture is visible as an unsupported
candidate and can be operated by coordinate input without eval. A decorative
fixture is not advertised as an actionable control. Conditional and masked
fixtures retain unknown eligibility rather than guessed truth. Discovery remains
bounded and progression neither skips decisions nor stalls on supported chrome.

#### 5. Broaden ending coverage without guessing completion

- [x] Exercise the existing lifecycle detector against custom end screens,
  explicit restarts, clean process exits, and crashes, alongside root returns,
  called scenes, replay, and initial menus. Keep root return plus confirmed
  main menu as the existing verified-ending rule.
- [x] Reuse lifecycle/progression and session-status results to distinguish a
  verified ending, an unresolved interaction, and process termination. An empty
  control list, inert frame, timeout, explicit restart, or successful process
  exit alone must not establish story completion. Preserve the last reliable
  observation and engine evidence when the process can no longer be queried.
- [x] Add broader automatic ending detection only where a supported engine
  signal actually proves it. Custom screens with no such signal remain an
  explicit limitation; do not match ending captions, labels, or game variables.
  Sandbox milestone completion remains separate from engine ending evidence.

Acceptance: positive ending evidence survives menu inspection, clears on a new
run/load, and is never manufactured for custom screens, exits, crashes, or
restarts. Existing public tools explain the observed boundary and preserve
uncertainty. This validation can run alongside steps 1–4.

#### Validation and documentation for each step

Use small independent Ren'Py fixtures for the engine behaviors, not Academy34
class names or route flags. Run meaningful unit/contract checks and the focused
live fixture on 8.0.3 and 8.5.3; run the full suite once the combined changes are
ready. Keep schema, registration, contract snapshots, README/MCP/architecture
notes, and changelog synchronized where public behavior changes. Explain unknown
geometry/eligibility explicitly in the agent workflow. The shared collector and
frame contract should remove compatibility workarounds from that workflow.

Keep Settings label polish deferred. Immediate disconnect cancellation remains
a separate backend follow-up: retain the engine lease until a tested cancellation
hook is available rather than adding another unverified cleanup path.

After these fixes, resume the isolated Academy34 checkpoint for a short
public-MCP acceptance check: enumerate/choose a menu item, inspect a free-roam
area without eval, click a returned-image point using its descriptor, and
save/load. This checks the repaired interfaces; completing the introductory
week or running another full playthrough requires a renewed live-test request.

### Implementation and acceptance results (2026-10-04)

The five generic delivery steps above are implemented. Control discovery and
legacy listings share one collector; gameplay frames retain source identity;
inspection uses completed-render geometry; opaque custom input stops automatic
progression; owned process exits are distinguished from verified story endings.
New screens that have not reached the completed render return `not_ready` rather
than pairing new controls with the previous screen's geometry/image. Layer order
also applies to modal coverage for semantic controls and custom candidates.
Boundary stop reasons are reconciled with the settled final observation; actual
budget/failure reasons are retained.

A short public-MCP Academy34 check resumed the copied Sunday-midnight checkpoint
under an isolated shadow project on Ren'Py **8.5.3**, Linux, the existing display:

- Listed Sleep/Cancel and selected Cancel using current observation guards.
- Observed the room's custom regions with `omitted.unclassified=true`, without
  evaluating game code or reading private action/route fields.
- Clicked the Sleep icon in the returned **799x450** image using its descriptor
  for the **1066x600** source/logical frame; the bridge hit logical **(700,355)**.
- Selected Cancel again, then verified progression stopped `unsupported` with
  **zero advances** and `ending=null`.
- Saved/loaded `mcp-interface-check` and stopped the owned process, removing its
  bridge artifacts. The original checkpoint was retained; story time did not
  advance. This does not complete the introductory week.

Evidence: `/tmp/renforge-academy-acceptance/` and
`/tmp/renforge-academy-acceptance-output.txt`; these local files are not durable.

Validation (Linux, existing display for live fixtures):

```bash
.venv/bin/pytest -q
# 1,136 passed, 70 skipped; five existing FastMCP deprecation warnings.
RENFORGE_SDK_TESTS=1 RENFORGE_SDK_VERSION=8.5.3 .venv/bin/pytest -q tests/test_integration_sdk.py
# 35 passed before the final modal/settled-boundary refinements.
RENFORGE_SDK_TESTS=1 RENFORGE_SDK_VERSION=8.5.3 .venv/bin/pytest -q tests/test_integration_sdk.py -k 'demo_control_supports_editor_save or custom_layer_scaled or coordinate_click_supports or modal_hide'
# 3 passed; initial modal-fixture progression transiently stopped none.
RENFORGE_SDK_TESTS=1 RENFORGE_SDK_VERSION=8.5.3 .venv/bin/pytest -q tests/test_integration_sdk.py -k 'modal_hide'
# 1 passed when rerun in isolation.
RENFORGE_SDK_TESTS=1 RENFORGE_SDK_VERSION=8.5.3 .venv/bin/pytest -q tests/test_integration_sdk.py -k 'modal_hide or native_skip_obeys_step_budget or bounded_progression_restores or ending_detection'
# 8 passed after reconciling boundary reasons with settled observations.
RENFORGE_SDK_TESTS=1 RENFORGE_SDK_VERSION=8.0.3 .venv/bin/pytest -q tests/test_integration_sdk.py -k 'custom_layer_scaled or coordinate_click_supports or terminal_boundaries or ending_detection or act_edits_input'
# 10 passed, 25 deselected.
.venv/bin/python -m compileall -q src tests
git diff --check
UV_CACHE_DIR=/tmp/renforge-uv-cache uv pip check --python .venv/bin/python
# 75 packages compatible.
```

The broader 8.0.3 probe also selected demo editor-save/alpha checks: those two
could not launch because the demo's `config.check_conflicting_properties` setting
requires a newer engine. The ten standalone fixtures passed. This is a limit on
that fixture matrix; the MCP does not rewrite incompatible game configuration.
Full compatibility across all games/platforms is not established.

Remaining limitations: custom handlers may have only enclosing full-frame bounds;
unknown conditions/masks cannot be turned into certified semantic targets. No
additional supported engine signal was found that proves completion on an
arbitrary custom end screen or process exit. These remain explicit uncertainty,
not game-specific inference. Legacy screenshot/crop tools retain their existing
contracts; use gameplay frame descriptors for bounded observation-image clicks.

Last updated: 2026-10-04

Scope baseline: RenForge `b74ad93a6e0208ade617f680b2bdc6c12d2f619c`

## Historical harness proposal

This proposal predates the delivered gameplay fixes. Descriptions of prior
behavior below are historical motivation, not current API documentation. Current
contracts and limits are in `docs/MCP.md` and `docs/ARCHITECTURE.md`. Automatic
shadow projects, declarative profiles, route runners, and checkpoint manifests
remain proposed work; they are not required for ordinary MCP play sessions.

### Goal

Turn RenForge's existing live-control MCP into a dependable end-to-end test
harness for kinetic and freeroam Ren'Py visual novels. An agent should be able
to launch a game without touching the player's normal state, operate its main
menu, advance narrative until a meaningful interaction boundary, inspect and
use visible controls, capture evidence, save and restore checkpoints, and run
a bounded route specification with an unambiguous pass or failure result.

This document is intentionally game-independent. Custom screen names, route
choices, and state markers belong in a declarative project profile or route
file, not in RenForge's generic runtime code.

## Existing baseline

RenForge already provides most of the low-level foundation: process lifecycle,
an authenticated in-game bridge, screenshots, game and screen inspection,
focusable UI enumeration, guarded semantic and coordinate clicks, dialogue
advance, choice selection, keyboard/text/scroll input, save/load/list,
waits, scenarios, autopilot, and Xvfb support.

The work below hardens and composes those primitives into nine changes. Each
numbered change is intended to map to one focused pull request against upstream.
A checkmark means both its automated tests and listed live acceptance proof are
complete.

## Scope and support boundaries

- The isolation target is normal Ren'Py save/persistent lookup and normal
  engine-generated project artifacts. A shadow project is not an OS security
  sandbox against arbitrary game Python that intentionally reads or writes
  other paths. Strong isolation from hostile or untrusted game code requires a
  separate container/sandbox design.
- Initial live support for this milestone is the existing SDK launch path on
  Ren'Py 8.5.3 with Linux/Xvfb. Distributed game executables, additional Ren'Py
  versions, and additional platforms require separate compatibility work and
  live evidence before RenForge claims support.
- Isolated runtime-test sessions do not enable source-editing features such as
  the Live Editor. Source-editing sessions and disposable test sessions have
  different safety contracts and must be reported distinctly.
- Custom displayables and minigames may remain unsupported. RenForge must report
  incomplete or unsupported discovery explicitly; it must not claim universal
  input coverage.

## The nine changes

### 1. Launch isolated test sessions from an owned shadow project and save namespace

- [ ] Make `savedir="temporary"` an effective, source-safe isolation mode.

**Why:** RenForge currently sets `config.savedir` from an environment variable
in an injected Ren'Py `init` block, after some Ren'Py versions may already have
initialized persistent data and save locations. It also injects bridge and Live
Editor files into the real project. A native save override without a shadow
project is incomplete, while a shadow project without an early save override
can still observe normal persistent state. These are one externally observable
isolation guarantee and must ship together.

**Implementation:**

- Add an explicit isolated test-session mode; `savedir="temporary"` selects it
  unless a future API provides a clearer equivalent.
- Build a per-session shadow project that preserves required source-relative
  paths while excluding `game/saves`, `.renforge`, caches, generated files, and
  configured user-state roots.
- Use copy-on-write reflinks only where their isolation semantics are proven,
  with a regular copy fallback. Do not use hardlinks or writable symlinks to
  source files that the game or engine could modify.
- Put injected bridge files, control metadata, logs, caches, compiled output,
  screenshots, and other writable runtime artifacts under owned session paths,
  not under the original project.
- Pass the temporary save directory using Ren'Py's global `--savedir` option in
  the correct position before the project base directory. Refactor the SDK
  command builder as needed rather than appending it as a `run` option.
- After the bridge starts, canonicalize and compare the requested directory,
  `config.savedir`, and every observable active Ren'Py save/persistent location.
  Fail closed if any active location is outside the owned session roots.
- Remove the injected late-init redirect once the supported SDK launch path uses
  the native override, or retain it only as a documented assertion/defense in
  depth that cannot weaken the native check.
- Avoid creating the original project's `.renforge` directory merely to validate
  or represent a project during an isolated launch.
- Inventory the original project and known normal save roots before and after
  the run. Retain a bounded diagnostic manifest and report whether integrity was
  preserved.
- Report the session ID, source project, shadow project, requested/effective save
  directory, isolation mode, ownership, cleanup policy, and integrity status in
  launch/status/stop results.
- Preserve explicit existing/default modes, but label them non-isolated. Do not
  imply that they protect player state.
- Cleanup may remove only paths with the active session's ownership token. Never
  rename, delete, or temporarily move the player's real saves.

**Acceptance:** A fixture places sentinel data in both the normal persistent
store and `game/saves`, then launches an isolated session on Ren'Py 8.5.3 under
Linux/Xvfb. Neither sentinel is visible. A session save exists only in the owned
temporary namespace. Starting, interacting, stopping, failed startup, explicit
cancellation, and interrupted-process cleanup leave the original project and
normal save roots byte-for-byte unchanged. Cleanup removes only
ownership-matched session paths, and the bridge-reported effective save locations all
resolve inside those paths.

### 2. Define and validate declarative project profiles

- [ ] Add a versioned, game-independent profile contract and resolver.

**Depends on:** Change 1 for pre-launch validation and session metadata.

**Why:** Ren'Py has conventional screens such as `main_menu` and `choice`, but
freeroam hubs, minigames, terminal states, and important state variables are
project-specific. Guessing them is unsafe, while hard-coding them in the MCP
makes RenForge useful for only one game.

**Implementation:**

- Support checked-in TOML profiles with a required schema version.
- Define marker categories for main menu, narrative choice, freeroam, terminal,
  and unsupported/custom interaction screens.
- Use declarative predicates such as active-screen, current-label, and bounded
  named-store-value comparisons. Do not accept arbitrary Python expressions or
  claim that general expressions can be proven read-only.
- Define progress policy such as whether future fast skip is permitted, whether
  unknown actionable UI is a stop condition, and default step/event/time limits.
- Resolve profiles explicitly or by one documented project-relative default.
  Include the canonical profile path, schema version, and content hash in
  isolated session metadata.
- Reject unknown fields, duplicate/conflicting markers, invalid predicates,
  unsafe variable paths, and invalid limits before process launch. Do not infer
  a missing freeroam or terminal marker.
- Provide a documented minimal profile and two fixture profiles with different
  freeroam markers.

**Acceptance:** Valid profiles load deterministically and produce stable hashes.
Two unrelated fixture profiles express a conventional main menu and choice plus
different freeroam markers. Invalid profiles fail before launch with stable
structured codes. Launching without a profile remains useful for low-level MCP
tools but does not claim marker or route completeness.

### 3. Return one coherent observation of the current interaction

- [ ] Add a high-level `renforge_observe` operation and generation guards.

**Depends on:** Change 2 for marker matching.

**Why:** Calling state, screen, choice, input, and screenshot tools separately
can combine data from different Ren'Py interactions. A screenshot content hash
can also repeat across distinct interactions, so it is not sufficient as a
monotonic interaction identity.

**Implementation:**

- Capture state in one in-bridge main-thread operation at a documented Ren'Py
  interaction boundary.
- Return current label, active screens, dialogue, narrative choices, currently
  known focusable controls, matched profile marker, logical resolution, and an
  optional screenshot in one bounded response.
- Define distinct identifiers: launch `session_id`, monotonic `observation_id`
  or interaction generation, screenshot `frame_hash`, and `event_cursor`.
- Attach session and observation identity to every action derived from an
  observation. Existing click and choice paths must reject identities from a
  prior session or interaction generation.
- If interaction state changes during collection, retry within a small fixed
  budget or return `OBSERVATION_UNSTABLE`.
- Include explicit `omitted`, `truncated`, `unsupported`, and screenshot-age or
  screenshot-relationship metadata rather than implying completeness.
- Keep existing granular tools for diagnostics and lower-token queries.
- When the process has crashed and cannot produce a fresh bridge observation,
  return the last successful observation separately from out-of-band process and
  crash diagnostics; do not label the combination as one atomic observation.

**Acceptance:** A live fixture changes screens rapidly while observations are
sampled. Every successful response contains mutually consistent marker,
controls, choices, state, and screenshot metadata; unstable samples are
rejected. A visually identical interaction still receives a new observation
generation when its actionable state changes. Click and choice tools reject IDs
from prior sessions and stale observations.

### 4. Inventory supported actionable inputs and report completeness

- [ ] Replace the flat focusable list with a normalized, read-only input inventory.

**Depends on:** Change 3 for coherent observation and generation identity.

**Why:** Visual novels use more than text buttons. A partial inventory is useful
only if it names its coverage boundary and does not silently present itself as
the complete set of possible actions.

**Implementation:**

- Define the initial roster as `choice`, `button`, `imagebutton`/`hotspot`,
  `bar`, `viewport_scroll`, `drag`, and
  `text_input`. Represent generic dialogue dismissal as an interaction
  capability rather than inventing displayable geometry when none exists.
- Return stable ID when available, visible text/action summary, screen/layer,
  logical bounds and center, enabled/sensitive state, z-order/coverage,
  clickability, supported operations, and the originating session/observation
  identity.
- Distinguish `supported`, `unsupported`, and `unknown` capabilities. An input
  may be visible and inspectable without being safely actionable.
- Expose completeness metadata for clipping, virtualization, traversal limits,
  custom displayables, unsupported transforms, and truncation.
- Do not add new mutating operations in this PR; keep it focused on trustworthy
  discovery and capability reporting.

**Acceptance:** A fixture screen exercises every input kind in the initial
roster, occlusion, disabled controls, duplicate text, scrolling, and a custom
unknown displayable. The inventory reports correct geometry and capability
status against one observation generation. The unknown/custom case produces an
explicit incompleteness signal rather than disappearing silently.

### 5. Safely actuate inputs reported by the inventory

- [ ] Add observation-guarded operations for the supported input capabilities.

**Depends on:** Change 4 for input IDs and capability reporting.

**Why:** Discovery and mutation have different review and safety concerns. An
agent should only invoke an operation that the current observation explicitly
reports as supported, without guessing among duplicate controls or replaying a
stale coordinate.

**Implementation:**

- Add one narrowly typed `renforge_act` dispatcher while preserving the existing
  low-level choice/click and text/scroll tools for compatibility and diagnostics.
  Every `renforge_act` call requires the originating session and observation
  identity plus an unambiguous input ID.
- Define typed operations for the initial roster: `select`, `click`,
  `enter_text`, `scroll`, `drag`, and `set_value`. Advertise and accept
  `set_value` only where the runtime exposes a safe bounded value operation.
- Require an explicit target when duplicate text or several controls match.
- Preserve guarded coordinate clicks for diagnostic/manual use, but do not let
  route replay infer coordinates from ambiguous text.
- Retain a narrow allowlist of focused Ren'Py key fallbacks such as Escape,
  Enter/Space, arrows, and page navigation. Do not expose arbitrary desktop
  shortcuts as route actions.
- Return a stable effect/result code and the next observation generation when
  available; never report success solely because an input event was posted.

**Acceptance:** Each supported inventory operation succeeds on a live fixture
and produces an observable effect. Disabled, occluded, ambiguous, unsupported,
prior-session, and stale-observation targets fail with stable codes. No fallback
can target another desktop window or invoke an unlisted shortcut.

### 6. Add bounded automatic progression to the next meaningful boundary

- [ ] Implement deterministic `renforge_advance_until` with structured stop reasons.

**Depends on:** Changes 2 through 5.

**Why:** Repeated one-line `renforge_advance` calls are slow and token-heavy.
Route automation needs one bounded operation that advances kinetic dialogue but
stops before a choice, main menu, freeroam hub, terminal state, crash, or
interaction it does not understand.

**Implementation:**

- Check a coherent observation before dispatching any input and stop immediately
  when it already matches a configured boundary.
- Keep repeated guarded dismiss actions as the explicit compatibility mode.
  Bounded native skip is now the default; see the current work checklist above.
- Wait for an observation-generation or event-cursor change instead of sleeping
  a fixed time.
- Enforce wall-clock timeout and step/event budgets and restore temporary
  operation state in `finally`-style cleanup. Defer MCP request/disconnect
  cancellation until the wrapper has a concrete, tested cancellation hook.
- Stop on unknown modal or actionable UI by default. Never select a choice or
  click a freeroam target automatically.
- Return the final coherent observation, stable stop code, elapsed time,
  step/event counts, progression mode, and bounded evidence metadata.

**Acceptance:** Live fixtures prove every initial stop reason: main menu,
choice, freeroam, terminal, unsupported UI, crash, timeout, budget exhausted,
and unstable observation. Progression reaches each boundary without
consuming a choice or freeroam action. Tests also cover transitions, pauses, and
a screen that never settles.

### 7. Harden checkpoints into isolated, verifiable session artifacts

- [ ] Add a namespaced checkpoint lifecycle and compatibility manifest.

**Depends on:** Changes 1 through 3; Change 6 supplies route-step context.

**Why:** RenForge can save, load, and list named slots, but route checkpoints
need collision-proof names, evidence, compatibility metadata, and a reliable
post-load bridge handshake. A successful file load alone does not prove that
the expected route state was restored.

**Implementation:**

- Define checkpoints as active-run artifacts for this milestone. Map user-facing
  names to sanitized, session-prefixed Ren'Py slots and reject traversal,
  reserved names, ambiguous names, and cross-session access.
- Support save, load, and list only inside the active isolated namespace.
  Session cleanup removes the namespace. Individual checkpoint deletion is
  deferred from this milestone.
- Before saving, request the Ren'Py save screenshot and create a sidecar manifest
  containing session/profile/route identity, Ren'Py version, defined script and
  archive hashes, label, marker, profile-allowlisted state, route step, save hash,
  and timestamp.
- Define the game hash precisely: include declared `.rpy`, `.rpyc`, and `.rpa`
  runtime inputs and exclude mutable evidence/output paths.
- After load, wait for bridge readiness, issue a new observation generation,
  observe the restored state, and validate manifest compatibility and declared
  state before reporting success.
- Store checkpoint evidence under the owned session directory while copying
  durable route evidence to the route output directory before session cleanup.

**Acceptance:** Checkpoints round-trip to the expected label, marker, and
allowlisted state after intervening gameplay. Loading missing, malformed,
modified, incompatible, or cross-session checkpoints fails with stable codes.
Original player save slots remain unread and unchanged. Cleanup removes the
owned checkpoint namespace without removing the durable route evidence bundle.

### 8. Build the route engine, evidence bundle, fixtures, and CLI

- [ ] Compose the hardened primitives into a versioned non-interactive route runner.

**Depends on:** Changes 1 through 7.

**Why:** Low-level MCP calls and the existing inline scenario runner make
exploration possible, but repeatable CI testing needs a deterministic oracle,
artifact trail, cleanup guarantee, and process exit status.

**Implementation:**

- Define a versioned route schema with steps for session start/stop,
  `advance_until`, expected markers/inputs/state, unambiguous semantic selection,
  checkpoint save/load, screenshots, and terminal assertions.
- Reuse the same profile predicates and input identities introduced earlier;
  do not add an independent expression or selector language.
- Require explicit selection when several controls match. Never let replay guess
  among ambiguous choices or coordinates.
- Emit a run manifest, ordered JSONL event log, observations, screenshots,
  checkpoint manifests, errors, integrity results, and concise JSON and human
  summaries. Hash evidence files from the run manifest.
- Write durable evidence to an explicit output directory outside the disposable
  session root so failure diagnostics survive cleanup.
- Fail the CLI non-zero on launch/bridge errors, crashes, timeouts, budget
  exhaustion, unexpected or incomplete UI, stale actions, missing evidence,
  assertion mismatch, cleanup failure, or source/save integrity failure.
- Add one kinetic and one freeroam fixture project and successful/broken routes
  for both. Exercise Ren'Py 8.5.3 on Linux/Xvfb; do not call this a multi-version
  or multi-platform compatibility matrix.
- Expose one non-interactive CLI command over the reusable route-engine module.
  Keep the existing inline `renforge_run_scenario` behavior compatible.

**Acceptance:** From a clean environment, one CLI command launches each fixture,
uses its main menu, advances to every declared boundary, enumerates and selects
the requested input, captures screenshots, round-trips a checkpoint, reaches
the declared terminal state, cleans up, and exits zero. Deliberately broken
routes exit non-zero with the exact failed step and retained evidence. After
either result, the source project and normal save roots pass integrity checks.

### 9. Expose the route engine through the public MCP contract

- [ ] Add a thin MCP integration over the route engine without duplicating execution logic.

**Depends on:** Change 8.

**Why:** Agents need the authoritative runner through MCP, while keeping public
contract changes separate from the route engine and CLI makes both reviews more
focused. MCP failures are structured results, not process exit codes.

**Implementation:**

- Add a new `renforge_run_route` tool that delegates to the reusable route
  engine while keeping `renforge_run_scenario` backward compatible.
- Accept a bounded route object or validated project-relative route path plus an
  explicit evidence-output policy. Reject traversal and unsafe output paths.
- Return stable run status, failed-step information, cleanup/integrity results,
  and bounded links/metadata for retained evidence. Do not inline an unbounded
  evidence bundle into the MCP response.
- Classify the invocation by its highest-risk route step in the operation-level
  policy, using the same authorization behavior as existing scenarios.
- Update the public tool definition, registration wrapper, tool-name catalog,
  contract snapshot, expected-tool tests, documentation, and policy tests in
  the same PR.

**Acceptance:** The successful and deliberately broken fixture routes from
Change 8 produce equivalent semantic results through CLI and MCP. MCP returns a
stable structured failure for the same failed step, policy denies unauthorized
destructive/open-world route operations before execution, and all public tool
contract tests pass.

## Delivery order and dependency policy

Deliver Changes 1 through 9 in order unless a PR explicitly proves that its
dependency is unnecessary. Do not merge placeholder public APIs whose behavior
depends on an unmerged later change. Internal schema fields intended for later
extension may be forward-compatible, but each PR must provide independently
useful, tested behavior.

Each numbered change above maps to one upstream PR. If implementation reveals
that a numbered change still contains two independently useful behaviors with
different safety or review concerns, update this roadmap and split the task
before opening the PR rather than silently expanding its scope.

## Upstream contribution requirements for every PR

Before implementation, open or link an upstream feature/design issue when the
change introduces a new safety contract, route/profile schema, or public MCP
surface. The isolation threat model and public profile/route contracts should be
agreed with the maintainer before their implementation PRs grow large.

Every PR must:

- State one coherent user-visible invariant, its dependency on earlier PRs, and
  its explicit non-goals.
- Link the relevant issue when one exists and describe breaking changes or write
  `None` when there are none.
- Add or update unit tests for behavior changes and include the listed focused
  live acceptance proof where Ren'Py behavior is involved.
- Report the exact commands actually run, including:

  ```bash
  python -m compileall src tests
  python -m pytest -q
  ```

- For a public MCP tool addition or schema change, keep the canonical
  `ToolDefinition`, matching module under `src/renforge/tool_registration/`,
  implementation under `src/renforge/tools/` or a focused helper, tool-name
  catalog, `tests/test_server.py` expected tools, and
  `tests/snapshots/mcp_public_tool_contract.json` synchronized, then run:

  ```bash
  python -m pytest -q tests/test_tool_definitions.py tests/test_tool_registration.py tests/test_server.py
  ```

- Record live validation with the exact Ren'Py version, OS, display mode, command,
  and result. The broad Ren'Py 8.5.3 SDK job runs on every pull request;
  the additional Live Editor workflow is opt-in via `live-tests`; a green default unit-test workflow is
  not a substitute for the task's live acceptance proof.
- Update relevant user and developer documentation in the same PR.
- Complete the upstream PR template's validation, user-facing changes,
  internationalization, breaking changes, documentation, reviewer notes, and
  final checklist sections. AI-assisted contributors remain responsible for
  understanding, validating, and explaining the submitted change.
- If a future PR touches the dashboard, use canonical English i18n keys, preserve
  real `zh-CN` translations, run `npm --prefix ui run build`, and leave generated
  `src/renforge/ui/static/` assets uncommitted. The nine changes above should not
  add dashboard scope unless the roadmap is explicitly amended.

## Deferred follow-up work

- MCP request/disconnect cancellation for long-running progression after a
  concrete FastMCP cancellation hook is identified and tested.
- Distributed game executable launch and bridge-injection support.
- A declared Ren'Py-version support policy and live oldest/newest compatibility
  matrix.
- Platform expansion beyond the existing Linux/Xvfb live target.
- OS-level sandboxing for hostile or untrusted game Python.
- Additional input adapters beyond the initial proven capability roster.
- Individual checkpoint deletion if route workflows demonstrate a need for it.
- Dashboard route authoring, execution, or evidence browsing.

## Out of scope for this milestone

- General desktop automation outside the Ren'Py window.
- Automatically solving arbitrary minigames or custom displayables.
- Inferring story correctness or the intended route without a route file.
- Pixel-perfect visual regression beyond capturing named evidence screenshots.
- Modifying, migrating, or deleting a player's normal saves.
- Claiming universal input completeness, hostile-code isolation, platform
  support, or Ren'Py-version support without corresponding implementation and
  live proof.
