# RenForge architecture

## Code layout

```
src/renforge/
  cli.py            # argparse entrypoint (inspect / serve / ui)
  server.py         # MCP app bootstrap + FastMCP/fallback; delegates tool registration
  tool_definitions.py # canonical 58-tool public contract (names, schemas, annotations)
  tool_schema.py    # shared JSON Schema fragments used by the contract catalog
  tool_registration/
    registry.py     # backend-aware ToolRegistrar; clones wrappers before annotating
    wrappers.py     # shared context, activity logging, PNG content helper
    project_analysis.py  # info, inspect, scan, lint, references
    lifecycle.py    # launch, jump, new_game, stop
    runtime_state.py # game state, eval, vars, wait, errors
    interaction.py  # control, input, saves, choices, clicks
    inspection.py   # screenshots, scene tree, measure
    scenarios.py    # run_scenario, autopilot
    content_build.py # assets, translations, builds, docs
  interaction.py    # pure gameplay contract shared with the injected bridge
  observe.py        # host image decoding + compatibility exports
  progression.py    # native skip, guarded dismiss, settling, process failures
  bridge/           # in-game .rpy bridge, launcher, and client
  tools/
    live.py         # running-game control (launch, eval, screenshot, ...)
    project_ops.py  # assets, translations, builds, docs
    static.py       # inspect / scan / parse-lint
  ui/               # Starlette dashboard (server, ws, graph, activity, poller)
  dashboard_client.py # private display-bound delegation to the dashboard
  image_ops.py       # local/live image crop and zoom primitives
  navigation.py      # shared label and file:line warp resolution
  session_registry.py # dashboard-to-MCP active-project discovery
  symbols.py         # Ren'Py-aware token/reference lookup
  policy.py          # operation-level risk classification and enforcement
  util/             # filesystem + subprocess helpers
  sdk.py            # Ren'Py SDK download/cache
  scanner.py        # script/label/asset scanning
  lint.py           # lint runner + parsing
  autopilot.py      # branch auto-play + coverage
  translation.py    # translation generation/stats
```

The dashboard frontend sources live in `ui/` (Vite + React + TypeScript) and
build into `src/renforge/ui/static/`. CI/release regenerates this directory
with `npm --prefix ui ci && npm --prefix ui run build`, then validates bundled
assets before packaging. Generated static files stay ignored in source checkouts
and are included in wheel/sdist artifacts so PyPI and `uvx` users never need Node.

## MCP tool registration

`server.py` only constructs the FastMCP (or compatibility) app and calls
`tool_registration.register_all_tools`. Each domain module owns a disjoint
`TOOL_NAMES` tuple and the corresponding wrapper bodies. `ToolRegistrar`
looks up the matching `ToolDefinition`, clones the wrapper so registration
never mutates shared `__annotations__` or `__doc__`, then applies description,
`ToolAnnotations`, and JSON Schema metadata. Registration is fail-closed:
unknown names, parameter drift, duplicate registrations, and backends that
cannot accept required metadata raise instead of shipping a partial catalog.

The public 58-tool contract is snapshotted in
`tests/snapshots/mcp_public_tool_contract.json`. Intentional API changes must
update that file; accidental ones fail CI.

## Live control flow

`renforge_launch` injects a temporary bridge into `<project>/game/` (removed on
teardown) and starts the game. If the matching dashboard is running, MCP
delegates launch to that process so it inherits the dashboard's display
environment; otherwise it launches directly (`display=auto` starts Xvfb and a
dummy SDL audio driver when no display is available). Fully headless CI can
wrap direct launches with `xvfb-run`.

MCP launch calls wait no more than 20 seconds. Slow startups continue in a
daemon launch task tracked per project; `renforge_launch_status` exposes the
`starting`, `ready`, or `failed` result, and `renforge_stop` signals the launch
task so the launcher terminates Ren'Py and removes injected artifacts.

The dashboard publishes its selected project in a per-user local runtime
registry. Agents call `renforge_info` or `renforge_context` first instead of
guessing the game path. `renforge_jump` resolves a label to `file:line` and
restarts through Ren'Py's supported warp path; `renforge_new_game` starts a
fresh process at the project's `start` label through that same path.

## Ren'Py SDK resolution

RenForge does not require a pre-installed Ren'Py. `sdk.py` first checks
conventional SDK locations inside the detected project and uses one only when
its launcher and version are compatible. It then checks the explicit
`RENPY_SDK_HOME` override before falling back to the managed
`~/.cache/renforge/sdks/` cache. Missing or invalid cached SDKs are installed
under an inter-process lock and published atomically. Override the stable
version with `RENPY_SDK_STABLE_VERSION`. Discovery and graph inspection never
rewrite files under an existing or shared SDK root, including read-only
installations.

## Graph inspection and `--json-dump`

Story Map uses Ren'Py's `compile --json-dump` for authoritative label
locations. The pinned default SDK is **8.5.3**. From at least Ren'Py **8.4.1**
through **8.5.x** and upstream master, `Script.namemap` is keyed by `Node`
(`self.namemap[node] = node`; `Node.__hash__` / `__eq__` use `.name`).
Released 8.5.3 `renpy/dump.py` still filters with `isinstance(name, str)`,
which drops every label. Upstream master `dump.py` unwraps `Node` keys
before that check; the namemap itself stays Node-keyed.

RenForge follows that dump unwrap without waiting on a new SDK release and
without patching installed `dump.py`. Each dump subprocess loads a temporary
`.rpe.py` adapter from `RENPY_SEARCHPATH` that wraps `renpy.dump.dump` and,
only when keys are not already strings, presents a string-keyed namemap for
the duration of the dump. Because namemap keys remain `Node` objects even
after upstream `dump.py` is fixed, the adapter still normalizes the map on
those SDKs. It is a no-op only when keys are already strings. Concurrent
inspections use separate adapter directories and cannot race on SDK source
contents.

Supported dump shapes:

- Node-keyed `namemap` on Ren'Py **8.4.1+** and **8.5.x** (including the
  default 8.5.3 SDK): the adapter presents a string-keyed copy for the dump
- String-keyed namemaps, if present: the adapter leaves the map unchanged
- Future SDKs whose `dump.py` already unwraps `Node` keys: namemap stays
  Node-keyed, so the adapter still normalizes it

## Real-engine test boundary

The default pytest suite is the fast unit/contract layer and does not launch
Ren'Py. `.github/workflows/ci.yml` adds one Linux/Xvfb integration job that pins
the default supported SDK, launches the real engine, and runs
`tests/test_integration_sdk.py` plus
`tests/test_integration_sdk_live_demo_acceptance.py`. The larger focused Live
Editor suite remains in `.github/workflows/live-editor.yml` because it has a
different cost and trigger policy.

Real-engine fixtures treat `examples/demo_game` as read-only input and copy it
to pytest-owned temporary directories before RenForge injects bridge/editor
files or Ren'Py writes caches and saves. CI caches only the SDK. Failure
artifacts use an explicit allowlist for logs, tracebacks, screenshots, and
autopilot output; the `control` state folder is never uploaded because it contains the
session credential.

The SDK test files retain their small local fixtures for this first CI layer.
Shared engine fixtures, markers, and additional-version smoke coverage should
be introduced only when they reduce demonstrated duplication or enable a real
compatibility claim.

## Packaging

Packaging uses `hatchling`; the console script is
`renforge = renforge.cli:main`. Optional dependency groups:

- `ui` — dashboard (starlette, uvicorn, watchfiles)
- `test` — pytest
- `fastmcp` — alias for the base install (fastmcp is a core dependency)

The server falls back to a compatibility mode with a clear message if
`fastmcp` is not installed (for example after a minimal manual install).

## Runtime policy

`policy.py` classifies high-risk MCP calls (`renforge_control`,
`renforge_saves`, `renforge_eval`, `renforge_run_scenario`) from the tool name
and validated parameters, then allows or denies them inside `_log_tool_call`
before the live implementation runs. MCP `ToolAnnotations` remain static
discovery hints; this layer is invocation-time enforcement. Defaults stay
permissive (`RENFORGE_POLICY=off`). See [POLICY.md](POLICY.md).

## Shared gameplay contract

`interaction.py` owns control IDs, role normalization, widget operation
projection, dialogue/readout cleanup, classification, and input guards. It uses
only the standard library and supports the Python runtime embedded in Ren'Py
8.0 onward. The host imports it normally. `bridge_payload()` embeds its exact
source into the one owned bridge artifact and executes it in an ordinary
module outside Ren'Py store/rollback. No installed RenForge package is required
inside the engine, and games cannot overwrite the contract's builtin names.
Bridge runtime tests execute this same assembled payload.

The bridge makes one bounded scene inventory per observation or action. Screen,
input, kinetic-control, and text projections reuse its nodes and ancestry.
An action reuses private widget references from that collection and omits
readout work. `operations` comes from actual available widget adapters; public
IDs and guards use the shared contract. Callback execution retains the owning
screen context. Viewport adapters validate both axes before applying changes.

Snapshots expose launch `session_id`, interaction generation, and a fingerprint
`observation_id` of the actionable inventory, including concrete widget identity.
Callers that send all three reject stale sessions and screen rebuilds; legacy
interaction-only callers retain compatibility. Tracked key-up releases are
allowed after an interaction changes, but remain session-owned.

Lifecycle classification reads engine main-menu context flags. A statement
callback records root script returns only outside menus, replays, and init, with
an empty return stack. Arrival at the engine menu confirms and latches the ending
on the non-rollback bridge object, emits one ending event, and exposes its return
location. Resuming root gameplay (including custom Start targets) or
start/load/warp/replay clears the evidence. The shared contract stops
both progression modes at `end` or `main_menu`, even without clickable menu
controls; the host uses a settled final observation before reporting completion.

The host composes action, observation, and bounded progression with one client.
`act` returns the next stable observation by default; optional progression
reuses `advance_until`. Input success and observation/progression failure stay
separate. Action follow-up observations do not advance the transcript cursor.
Say callbacks assign occurrence IDs, so repeated words remain distinct and
frame/event copies merge. Rendered dialogue takes precedence over raw callback
text from older engines, including interpolation. Event polling reports
bounded-buffer gaps.

## Bounded native progression

`progression.py` orchestrates an owned `progression` bridge lease. Default
`advance_until` and composed `act(advance=true)` enable native slow skip rather
than issuing one socket request per dismiss. Fast skip mode is deliberately
excluded because Ren’Py can bypass interactions in that mode. At every new
interaction the bridge freezes skip before screen construction; the periodic
main-thread inventory resumes it only for known narrative. The lease counts
advances and checks deadlines inside the game. Choices, inputs, custom controls,
unknown interactions, exceptions, completion, and lease expiry disable owned
skip and restore the saved preferences. `mode="dismiss"` keeps the guarded
single-step path in the same host module; `observe.py` retains compatibility
exports and handles delivered images.

The host polls events during progression and reserves part of the total budget
for operation cleanup, two matching stable final observations, image capture,
and final event retrieval. Lost input replies carry an unknown action outcome;
frame timeouts and transport failures do not imply a game crash. Historical
observations are kept separately when no final frame settles.

Gameplay PNGs are bounded to 384 KiB by `util/images.py`. `frame_hash` hashes
the exact delivered PNG; `frame_id` identifies the original engine capture.
`observation_id` identifies interaction/control state independently of those image
hashes. Logical control geometry is independent of image scaling.

### Shared gameplay discovery and frames

The bridge collects controls once from engine focus records, supported widgets,
and active screen menu entries across all runtime layers. Observation and the
legacy list/action tools reuse those records and canonical IDs. The collector
assigns widget facts and IDs once, using named widgets from actual owning screen
instances rather than another lookup by screen name. Menu actions
are associated by identity and invoked in their owning screen context; captions
provide displayed text rather than matching keys. Observation identity includes
live widget and action identities, guarding rebuilt controls even when captions
remain identical.

A bounded walk of the completed Render graph joins `render_of` identities to
logical geometry, composing reverse transforms, offsets, and clipping. Repeated
placements retain render paths. Inspection neither runs displayable input nor
calls displayable render methods. Alpha inspection rasterizes cached renders.
Perspective or unavailable geometry remains explicit. Known focus/mask checks
prove candidate click points; failing a center probe does not prove full coverage.

Unhandled custom event implementations are reported as uncertain input regions.
No game class names, action-field conventions, flags, or labels classify them.
The shared progression boundary and dismiss guard stop on incomplete discovery.
Engine adapters and decorative displayables keep their established behavior.

Gameplay screenshots preserve a source `frame_id` independently of the delivered
PNG's `frame_hash`. The frame descriptor travels with the returned image and
carries source/session and pixel/logical dimensions. Coordinate input validates
that descriptor and converts returned pixels directly to logical coordinates;
no mutable last-screenshot cache or Pillow dependency is introduced in the SDK.
Process exit evidence comes only from a matching retained owned process and
never establishes story completion. Existing root-return/main-menu ending
confirmation remains unchanged.
