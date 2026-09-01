# RenForge End-to-End Minimal Play — Implementation Plan

> **For agentic workers:** implement this plan task-by-task. Keep checkbox state in this document, use focused tests before broad suites, and do not launch a real user game until both proof gates in Tasks 0 and 11 pass.

**Goal:** prove that an agent can play one complete click/advance-compatible route through a real Ren'Py 8 development project while seeing coherent decision frames, resuming from isolated state, recording a durable trace, and leaving normal saves and the host project untouched by the running game.

**Architecture:** retain the user-supplied unpacked project as the editable source of truth, but launch its bundled Ren'Py runtime through a proven Linux isolation backend selected by an initial hostile-process spike. The preferred backend is a Bubblewrap copy-on-write overlay; a bounded full-copy runtime is the correctness fallback if unprivileged overlay is unavailable. Mount only an allowlisted filesystem, bind a named RenForge profile over every durable Ren'Py state location, inject the bridge only into the disposable runtime layer, attest the live paths before reporting readiness, and expose one atomic multimodal observation plus one guarded minimal action transaction. The agent remains the decision maker; RenForge owns isolation, evidence, and fail-stop recovery.

**Tech stack:** Python 3.11+, FastMCP/MCP content blocks, Ren'Py 8 bundled runtimes, injected Ren'Py Python/screen APIs, Bubblewrap isolation on Linux, JSONL traces, pytest, and opt-in real-engine tests.

---

## 1. Product decision and milestone relationship

This plan is **MVP 1: Safe Eyes and Hands**. It is a feasibility milestone for **MVP 2: Route-Assisted Playtest**.

MVP 1 proves the substrate:

```text
preflight
  -> isolated profile
  -> sandboxed bundled-runtime launch
  -> live isolation attestation
  -> atomic observe
  -> guarded click or advance
  -> stable-change observation
  -> durable evidence/checkpoint
  -> agent-declared completion
```

MVP 2 will add route matching, predetermined selections, expected-next-state assertions, divergence handling, and replay reports. MVP 1 must not invent a route schema or a general game-adapter framework, but its trace schema reserves optional route and adapter identifiers so those features can be added without rewriting evidence.

For MVP 1, “play the entire game” means one complete reachable route from a clean isolated profile to an agent-declared ending. The agent may choose freely or follow choices supplied out of band by the caller; RenForge records those decisions but does not interpret a route list.

### MVP 1 exit claim

The only compatibility claim made by this milestone is:

> On Linux, for the explicitly tested bundled Ren'Py 8 game/version and a user-provided development project containing loose `.rpy` source, RenForge can isolate play state and runtime writes, provide atomic visual/actionable observations, execute guarded mouse clicks or dialogue advance, resume the same isolated profile, and preserve a complete one-route trace.

It does not claim support for every Ren'Py 8 minor version or every interaction style.

### Critical review disposition

Three independent reviews were applied before this plan was finalized:

| Perspective | Critical finding | Plan disposition |
| --- | --- | --- |
| Isolation and recovery | Project-local control/state and a read-only bind of host `/` would still expose normal saves and trusted metadata; PID-only recovery and live reattachment are unsafe. | Put all authoritative state outside the project, start with an empty allowlisted namespace, expose only guest publication, use process-group/PID-namespace fail-stop ownership, and quarantine unverifiable recovery. |
| MCP and Ren'Py runtime semantics | Project-scoped live calls can target a successor session; screen hashes cannot distinguish identical consecutive interactions; observe immediately after act can return the old frame. | Require `session_id` and opaque `observation_id`, track outer/restart generations plus a semantic signature, capture image/semantics atomically, and implement change-then-settle observation. |
| Delivery and acceptance | The original surface was too broad and assumed the hardest isolation mechanism before proving it; “clearly see” and “entire route” lacked executable acceptance criteria. | Add hostile-process Gate A, controlled Ren'Py Gate B, then one local real route; defer reset, annotated frames, rich lifecycle APIs, adapters, and route automation; assert native-frame choice visibility and a deterministic checkpointed route. |

These are scope decisions, not implementation suggestions: later task breakdowns must preserve them unless this plan is explicitly amended.

---

## 2. Locked scope

### In scope

- Linux host only.
- One active minimal-play session per canonical project.
- User-provided complete development project with loose, unpacked `.rpy` files.
- Game's bundled Ren'Py 8 runtime and launcher.
- Named empty isolated profiles and reuse of an existing isolated profile; a strict session must be unable to read or enumerate any normal-play save location, not merely unable to write it.
- A Linux isolation backend proven by the Task 0 spike: Bubblewrap overlay when supported, otherwise a size-preflighted full-copy runtime inside Bubblewrap.
- Primary saves, game-local saves, persistent data, and MultiPersistent isolated per profile; home/XDG/cache/temp are private per session unless a real compatibility requirement later proves they must persist.
- Bridge injection only into the disposable runtime layer.
- Native-resolution screenshot, active screens, dialogue, current label, menu choices, and visible focusable controls captured atomically.
- Raw screenshot and matching semantic manifest in one MCP result.
- Guarded element click, guarded logical-coordinate click, and guarded dialogue advance.
- Interaction-stability wait based on semantic state rather than pixel equality.
- Durable observation/action journal and isolated Ren'Py save checkpoints.
- Agent-declared success, intervention, or failure through one finish marker.
- One complete click/advance-compatible real-game route as the final local acceptance.

### Explicitly out of scope

- Ren'Py 7, Python 2 bridges, Unity, macOS, or Windows.
- RPA extraction or RPYc decompilation; source preparation is a user prerequisite.
- Managed-SDK fallback for packaged-game play.
- Profile reset or importing the user's normal saves into an isolated profile.
- Determining whether a supplied path is the user's normal installation; strict isolation does not depend on making that inference.
- Live Editor, hot reload, or promotion of runtime-overlay writes back to source.
- Text entry, arbitrary keyboard input, drag/drop, controllers, realtime input, or minigame automation.
- Generic exhaustive autoplay, branch coverage, or clicking every visible control.
- Route plans, automatic decision selection, or game-specific adapters.
- Automatic ending detection or automatic walkthrough-quality scoring.
- Network isolation. The mount namespace blocks known filesystem exposure, but MVP 1 does not claim containment against malicious game code or shared-network abuse.

### Scope enforcement

- A route that reaches an unavoidable unsupported interaction does not fail the architecture. RenForge records `needs_intervention` with the last coherent observation and checkpoint.
- A real game must not be launched merely to discover whether isolation works. Gate A proves the backend with a hostile synthetic subprocess; Gate B proves Ren'Py save/bridge behavior with a controlled fixture; only then may a real game run.
- Cross-platform abstractions may be named in interfaces, but only Linux, Xvfb, and dummy audio are implemented in MVP 1 strict sessions.
- Each release names an exact tested Ren'Py 8 version. Other Ren'Py 8 versions are rejected by default or require an explicit unsupported-version opt-in that is visible in launch status and the trace.
- Existing legacy `renforge_launch` behavior remains outside the minimal-play safety claim until explicitly migrated. Documentation must never describe legacy `savedir=temporary` as equivalent to the strict profile path.

---

## 3. User-provided project contract

The user prepares a development project outside RenForge:

```text
example-game-dev/
├── game/
│   ├── script.rpy
│   ├── screens.rpy
│   ├── unpacked-source/*.rpy
│   └── walkthrough_mod.rpy
├── renpy/
├── lib/
└── ExampleGame.sh
```

The project contains game/mod source only. All authoritative RenForge play state lives in a separate private state root selected through `RENFORGE_PLAY_STATE_ROOT` or a platform user-state default. During a live session the game sees the project as a read-only lower/source tree. The agent may edit `.rpy` files on the host between launches; MVP 1 requires stop/relaunch to test changes. A launch-time source fingerprint marks the session `tainted` and prevents further actions or successful trace completion if tracked `.rpy` files change while the session is active.

Preflight must verify, without executing game code or creating `.renforge`:

1. canonical project root and `game/` are directories without unsafe path indirection;
2. at least one loose `.rpy` exists;
3. a single supported bundled launcher can be selected or the caller supplied one explicitly;
4. the bundled engine reports Ren'Py major version 8 through static version files;
5. the launcher, `renpy/`, and required platform libraries remain beneath the project root;
6. the isolation backend selected by the hostile-process spike is available and passes a disposable probe;
7. the requested profile ID is a safe component and is not locked by another session;
8. no existing unowned bridge/control artifacts would be overwritten.

Preflight must clearly state that unpacked-source completeness and fidelity are user responsibilities. It validates presence, not equivalence to an archive.

---

## 4. Identity and filesystem model

### Stable identifiers

- `project_id`: digest of the canonical project path only; mutable source/runtime fingerprints are recorded separately and never key profile ownership.
- `profile_id`: user-selected safe name for isolated play state.
- `session_id`: random identifier for one operating-system process lifetime.
- `trace_id`: identifier for one logical playthrough across one or more sessions.
- `frame_id`: SHA-256 of exact native PNG bytes.
- `observation_id`: opaque session-bound token naming one actionable observation.
- `ui_generation` and `restart_generation`: bridge-maintained monotonic Ren'Py interaction counters.
- `ui_signature`: digest of actionable semantic state, independent of animated pixels.

Every live response and trace event carries `project_id`, `profile_id`, `session_id`, and `trace_id`. Every actionable observation additionally carries `observation_id`, both generation counters, `ui_signature`, and `frame_id`. Public actions guard on `observation_id`; agents do not reconstruct or combine the internal counters themselves.

### External RenForge state root

```text
<renforge-state>/<project-id>/
├── project.json
├── locks/
├── profiles/<profile-id>/
│   ├── profile.json
│   ├── primary-saves/
│   ├── game-saves/
│   └── multipersistent/
├── sessions/<session-id>/
│   ├── launch.json
│   ├── upper/
│   ├── work/
│   ├── runtime/
│   ├── home/
│   ├── xdg-config/
│   ├── xdg-cache/
│   ├── xdg-data/
│   ├── tmp/
│   ├── guest-publication/
│   └── diagnostics/
└── traces/<trace-id>/
```

The state root is owned by the current user, mode `0700`, canonically outside the project, and validated with no-follow/private-path primitives. Profiles persist only intentional play state. Session home/XDG/cache/temp and runtime data are disposable so arbitrary custom writes cannot silently influence later sessions.

Session metadata follows a durable state machine: `prepared -> starting -> ready -> stopping -> stopped`, with terminal `failed` and quarantined `recovery-required` states. Acquire locks in the fixed order project, profile, then session metadata; release them in reverse. Only one live session may own a canonical project, preventing a successor session from making stale session-scoped commands ambiguous.

### Per-session runtime layout

The isolation backend presents the project at its canonical logical path inside the guest while redirecting all project-path writes into a session runtime. The overlay backend uses the session `upper/` and `work/`; the fallback copies the project into `runtime/` after disk/size preflight. No runtime or upper layer is ever synchronized back to the host project.

### Control-channel seam

The host MCP must discover the bridge without exposing trusted locks or launch metadata to game code. Host-owned `launch.json`, leases, locks, PID identity, and trace files remain outside the sandbox. Give the guest only one fresh session-specific `guest-publication/` directory, bind it at `/run/renforge`, and pass that path through `RENFORGE_BRIDGE_PUBLICATION_DIR`.

Do not derive control metadata from sandbox `project_root/.renforge`. The current project-local bridge metadata and manifest ownership code must separate trusted host launch state from the single guest bridge-ready publication while retaining no-follow/private-file guarantees. The bridge token authenticates local RPC clients; because game Python receives the token, it does not make guest-supplied attestation a security proof.

The current TCP bridge uses host loopback. MVP 1 shares the network namespace and must not pass `bwrap --unshare-net`.

---

## 5. Isolation contract

### Launch environment

Construct an allowlisted child environment rather than cloning `os.environ`. Build an empty Bubblewrap mount namespace rather than applying a read-only bind of `/`; a read-only real home would still violate the guarantee by exposing normal saves for reading.

Required variables include only:

- display/audio variables selected by the existing environment resolver;
- locale variables required for correct text rendering;
- platform/runtime variables required by the bundled launcher;
- session-private `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, and `TMPDIR`;
- `RENPY_MULTIPERSISTENT` pointing inside the profile;
- `RENPY_PATH_TO_SAVES` pointing inside the profile when the bundled version supports it;
- bridge token, session identity, control directory, and logical project identity.

Invoke the bundled launcher with native `--savedir <profile>/primary-saves`. Do not rely on an injected init block to set `config.savedir`.

### Filesystem exposure

Inside Bubblewrap:

- empty root namespace with only explicit mounts;
- project logical path: isolated runtime view backed by read-only lower plus disposable writes, or the proven full-copy fallback;
- runtime `game/saves`: bind to profile `game-saves/`;
- primary saves and MultiPersistent: profile-owned;
- home/XDG/temp: session-owned and disposable;
- required `/usr`, `/bin`, `/lib*`, selected `/etc`, minimal `/dev`, private `/proc`, and Xvfb socket exposure only as proven necessary;
- MCP workspace, SSH credentials, unrelated home directories, and normal Ren'Py save roots: not mounted.

Each strict MVP session owns a fresh Xvfb display and uses dummy audio; no host X11/Wayland/Pulse socket is exposed. Native display/audio integration is deferred.

### Process ownership

All strict launches and their session-owned Xvfb receive dedicated ownership; the game runs in a host POSIX process group plus a Bubblewrap PID namespace, reaper, and `--die-with-parent`. Stop performs bounded graceful termination and group/namespace kill escalation, reaps the leaders, confirms no owned descendants remain, then releases the profile lease and removes disposable runtime data. MVP 1 is fail-stop: an MCP-owner crash must terminate the game and its Xvfb; later runs recover durable profile/trace state by starting a new process, not by reattaching to the old one.

### Live attestation

Before launch reports `ready`, a bridge command returns:

- live Ren'Py version;
- `basedir` and `gamedir`;
- `config.savedir`;
- every load/save location recognized by `renpy.loadsave.location`;
- `config.extra_savedirs`;
- MultiPersistent root when observable;
- home/XDG/temp values visible to game Python;
- bridge control path;
- the logical/runtime/host path map used by the session.

The host validates every path against an explicit allowlist of runtime/profile roots. Host-side mount construction plus a controlled canary proves that a write at a project path lands only in disposable runtime storage and leaves the lower project unchanged. Bridge attestation is semantic corroboration, not the filesystem security proof. An unobservable Ren'Py save location is a failed attestation.

### Crash recovery

Profile leases and session metadata are durable. On server startup or the next profile operation:

1. validate lease ownership and recorded PID/start identity;
2. distinguish live owned process, dead process, and unverifiable process;
3. never kill a PID on numeric identity alone;
4. expect `--die-with-parent` to make prior processes dead rather than promise reattachment;
5. clean dead-session runtime data only after proving ownership;
6. refuse profile reuse while a live or unverifiable lease remains;
7. preserve bounded diagnostics and the trace tail before cleanup;
8. retain runtime data as `recovery-required` when death or ownership cannot be proved.

---

## 6. Public MCP contract

MVP 1 adds small, orthogonal public surfaces and extends strict launch. Exact JSON Schema is locked in Task 1 before implementation. Every live tool is session-scoped; a project path is used only for preflight, profile administration, and launch.

### `renforge_play_preflight`

```text
renforge_play_preflight(project_path, launcher_path="")
```

This is a read-only operation. It returns canonical project identity, selected launcher, statically detected engine version, loose-source presence, selected isolation backend, required disk space, and structured refusal/warning codes. It always reports unpacked-source completeness as `unverified`.

### Profile administration

```text
renforge_profile_create(project_path, profile_id)
renforge_profile_list(project_path)
renforge_profile_inspect(project_path, profile_id)
```

`create` makes a new empty isolated profile and refuses collisions. `list` returns bounded metadata for all profiles. `inspect` reports one profile and its lease without reading save payloads. Profile reset and normal-save copy/import are not MVP 1 actions.

### Strict `renforge_launch`

Add:

```text
profile_id: required for isolation="strict"
isolation: "strict" | "legacy"
runtime: "bundled" | existing SDK selector
launcher_path: optional explicit bundled launcher
record_trace: bool
trace_id: optional existing active trace
```

MVP 1 workflow always uses `isolation="strict"`, `runtime="bundled"`, and `editor=false`. Strict mode rejects `savedir`, `persistent`, arbitrary extra environment, managed SDK selection, and an absent profile.

Strict launch never delegates through the dashboard and never attaches to or reuses an existing process, legacy session, or externally launched bridge. It refuses any conflicting live project lease. Allocate and durably record `session_id` before starting background work so startup progress and startup failure remain queryable by that ID.

The response adds project/profile/session/trace identities, lifecycle state, selected backend, exact detected engine version, source fingerprint, and the full validated attestation summary. Existing legacy launch behavior remains available for compatibility but is explicitly outside the strict-play guarantee.

### `renforge_observe`

```text
renforge_observe(
    session_id,
    after_observation_id="",
    settle_ms=400,
    timeout=10.0,
)
```

Returns ordered MCP content blocks:

1. a text/JSON manifest;
2. the exact native-resolution raw PNG as an MCP image block.

When `after_observation_id` is supplied, observation first waits for a semantic UI change from that observation, then waits for the new semantic state to remain unchanged for `settle_ms`. Timeout returns the latest coherent observation with explicit `changed` and `stable` flags; it does not pretend the action took effect.

The manifest contains:

```json
{
  "ok": true,
  "project_id": "...",
  "profile_id": "playtest",
  "session_id": "...",
  "trace_id": "...",
  "observation_id": "...",
  "frame_id": "...",
  "ui_generation": 14,
  "restart_generation": 2,
  "ui_signature": "...",
  "changed": true,
  "stable": true,
  "classification": "dialogue|choice|controls|transition|unknown",
  "logical_size": {"width": 1920, "height": 1080},
  "capture_size": {"width": 1920, "height": 1080},
  "current_label": "chapter_one",
  "dialogue": {"who": null, "what": "..."},
  "screens": [],
  "menu_choices": [],
  "elements": [],
  "event_cursor": 0,
  "completeness": {"truncated": false, "reasons": []}
}
```

The raw screenshot and semantic data are collected by one main-thread bridge request. The manifest is bounded, but element text, identity, role, enabled state, and bounds must remain sufficient to interpret a choice without a second introspection call. Annotated or indexed screenshots are deferred; the agent sees the actual user frame.

### `renforge_act`

```text
renforge_act(
    session_id,
    observation_id,
    action,
)
```

Allowed action shapes:

```json
{"type": "click_element", "element_id": "..."}
{"type": "click_at", "x": 840, "y": 612, "coordinate_space": "logical"}
{"type": "advance"}
```

The bridge verifies the session and observation immediately before dispatch. `click_element` additionally proves that the element is still present and enabled. Occlusion/coverage may be returned as a conservative hint, but is not a hard rejection because Ren'Py transforms and masks make a generic coverage proof unreliable. A stale action is rejected and never retried automatically.

The result carries a correlation ID, resolved target, before observation ID, immediate business events, and `next="observe"`. The normal next call is `renforge_observe(session_id, after_observation_id=<acted observation>)`, which prevents an immediately queued observation from returning the pre-action screen.

### Existing save/stop tools and one finish marker

```text
renforge_play_finish(session_id, observation_id, status, reason)
```

Strict launch with `record_trace=true` creates the trace. Existing save/load and stop tools are reused, but strict-mode calls require `session_id` and reject a stale/successor session; one named save is the MVP checkpoint. `renforge_play_finish` accepts only `complete`, `needs_intervention`, or `failed`, requires a final coherent observation and reason, and closes the evidence journal. Trace inspection is a read-only status view, not a lifecycle command family.

---

## 7. Atomic observation contract

### One engine transaction

The injected handler must capture, in one main-thread request:

- native screenshot bytes and hash;
- current label and last dialogue callback data;
- active screen names, layers, modal hints, and z-order where Ren'Py exposes them reliably;
- active Ren'Py menu `items` and their visible matching controls;
- the complete visible focus list up to explicit limits;
- control IDs, text, role, screen, bounds, center, enabled state, and optional occlusion hint;
- event cursor and dropped-event metadata;
- logical/window/capture coordinate information.

The event ring gains a base cursor/dropped count so a long playthrough can detect loss instead of silently assuming continuity.

Hook `config.start_interact_callbacks` to increment `ui_generation` for each outer interaction and `config.interact_callbacks` to increment `restart_generation` for interaction restarts. Also compute a bounded `ui_signature` from actionable semantics. The counters distinguish two successive interactions that render identical dialogue or controls; the signature allows animation-only frames to settle.

Atomicity means the main-thread handler reads both counters, captures semantics and the PNG, then reads the counters again. If either counter changes during capture, it returns `consistent=false`; the host retries up to a bound and otherwise returns the latest observation as unstable. Never combine a screenshot from one bridge call with semantics from another.

### Stable element identity

Prefer, in order:

1. explicit screen/widget ID;
2. screen plus source location when runtime exposes it;
3. screen plus safe action descriptor plus normalized visible text;
4. deterministic frame-local fallback marked `stability="frame"`.

The observation must not pretend a synthetic ordinal is durable. A guarded action may use a frame-local ID only against the exact `observation_id` that produced it.

### Observation identity

`observation_id` is an opaque, session-bound reference to the coherent tuple of generations, semantic signature, and element table. It excludes screenshot pixels. Its internal semantic signature includes only bounded normalized values that affect available actions:

- current label;
- dialogue identity;
- active game screens;
- menu captions and enabled state;
- visible focusable identity, bounds, enabled state, and optional occlusion hint;
- modal ownership.

Animations may change `frame_id` without changing `observation_id` while the semantic tuple remains unchanged. A new outer interaction or restart changes the observation identity even if visible text is identical.

### Stability

Without `after_observation_id`, observe polls coherent transactions until the semantic signature and counters are unchanged for `settle_ms`, bounded by `timeout`. With `after_observation_id`, it first requires a generation/signature change and only then starts the settle window. It returns the latest complete observation even when change or stability is not reached.

Do not require pixel equality. Do not perform a long blocking sleep inside the bridge main thread.

---

## 8. Trace and checkpoint contract

### Trace layout

```text
<renforge-state>/<project-id>/traces/<trace-id>/
├── trace.json
├── events.jsonl
├── observations/<observation-id>.json
├── frames/<frame-id>.png
└── checkpoints/<checkpoint-id>.json
```

PNG data is never embedded in JSONL. Frames are content-addressed and deduplicated.

### Required events

- `trace.started`
- `session.launching`
- `session.attested`
- `observation.recorded`
- `action.requested`
- `action.rejected`
- `action.dispatched`
- `checkpoint.saved`
- `checkpoint.loaded`
- `runtime.exception`
- `session.stopped`
- `trace.finished`

Every action references the exact observation ID on which it was based. Every successful action stores its pre-action observation and frame. Failures store the latest available diagnostic observation. The single `trace.finished` event carries `complete`, `needs_intervention`, or `failed`; it cannot be rewritten as a different outcome.

### Reserved MVP 2 fields

Trace events may contain null/absent:

```json
{
  "route_id": null,
  "route_step": null,
  "decision_id": null,
  "adapter_id": null,
  "adapter_revision": null
}
```

No MVP 1 code branches on these fields.

### Retention

Default retention keeps:

- initial attested observation;
- every pre-action frame/manifest;
- every rejection/failure frame;
- checkpoint frames;
- final/intervention frame.

Enforce configurable per-trace byte and frame-count limits. When a limit prevents retention, the journal records exactly what was omitted and why. Never silently discard journal events.

---

## 9. Implementation tasks

### Execution order and later work-package rule

```text
Task 0 (backend proof)
  -> Tasks 1-3 (contracts, preflight, ownership)
  -> Tasks 4-6 (sandbox, launch, attestation)
  -> Tasks 7-9 (observe, expose, act)
  -> Task 10 (evidence/checkpoint integration)
  -> Task 11 (Gate B fixture)
  -> Task 12 (real-game proof)
```

Tasks are epics, not single-agent assignments. Before implementation, split each task into independently testable packages that own a narrow file set and produce a reviewed contract, test fixture, or implementation seam. Do not parallelize two packages that both edit `bridge.rpy`, `tools/live.py`, or the public tool snapshot; serialize those integration points. Every package begins with its focused failing test and ends with the task's existing regression tests, while Gate A, Gate B, and the real-game proof remain ordered and indivisible.

### Task 0: Prove the isolation backend against a hostile subprocess

**Files:**

- Create: `scripts/spike_play_isolation.py`
- Create: `tests/test_play_isolation_spike.py`
- Create: `docs/superpowers/reports/play-isolation-spike.md`

**Steps:**

- [ ] Build a disposable lower project, normal-save tree, profile tree, guest-publication directory, and session-private home/XDG/temp tree with distinct canaries.
- [ ] Probe an empty allowlisted Bubblewrap namespace using the overlay backend; separately probe a size-preflighted full-copy runtime fallback.
- [ ] From a hostile child, attempt to enumerate/read/write normal saves, lower project files, RenForge trusted state, the broader home/workspace, and undeclared host paths.
- [ ] Attempt intended writes to project paths, `game/saves`, native `--savedir`, MultiPersistent, guest publication, home/XDG, and temp; prove where every byte lands.
- [ ] Fork, double-fork, call `setsid`, and hold files open; prove stop/owner death terminates all descendants and makes cleanup boundaries unambiguous.
- [ ] Exercise unsafe symlinks, FIFOs, bind targets, missing overlay support, insufficient copy space, preflight failure, and diagnostic preservation.
- [ ] Record the exact kernel, Bubblewrap, filesystem, and selected backend result. If neither backend meets the contract, stop this plan and revise the architecture before public APIs are implemented.

**Gate A:** one backend proves that the guest cannot enumerate or read normal saves or trusted control state, cannot mutate the lower project, can persist only designated profile state, and leaves no descendants. This test is automated and remains a release gate.

### Task 1: Lock contracts and failing public-schema tests

**Files:**

- Create: `src/renforge/play/__init__.py`
- Create: `src/renforge/play/contracts.py`
- Modify: `src/renforge/tool_definitions.py`
- Modify: `src/renforge/tool_registration/registry.py` only if mixed-content typing requires it
- Modify: `tests/snapshots/mcp_public_tool_contract.json`
- Create: `tests/test_play_contracts.py`
- Modify: `tests/test_tool_definitions.py`
- Modify: `tests/test_tool_registration.py`

**Steps:**

- [ ] Write typed internal contracts for project/profile/session/trace identity, preflight, attestation, observation, element, action, and trace event.
- [ ] Add failing tool-definition tests for `renforge_play_preflight`, `renforge_profile_create`, `renforge_profile_list`, `renforge_profile_inspect`, `renforge_observe`, `renforge_act`, and `renforge_play_finish`, plus strict launch parameters.
- [ ] Lock enum values, session/observation guards, required fields, size bounds, timeouts, and risk metadata.
- [ ] Ensure create/finish risk classification is explicit in `policy.py` tests before implementation.
- [ ] Regenerate the public contract snapshot only after reviewing the exact diff.

**Gate:** schema and registration tests pass with stub wrappers; no live behavior exists yet.

### Task 2: Make project preflight read-only and detect bundled Ren'Py 8

**Files:**

- Modify: `src/renforge/project.py`
- Modify: `src/renforge/sdk.py`
- Create: `src/renforge/play/preflight.py`
- Create: `tests/test_play_preflight.py`
- Modify: `tests/test_project.py`
- Modify: `tests/test_sdk.py`

**Steps:**

- [ ] Write failing tests proving inspection does not create `.renforge` or any other path.
- [ ] Separate canonical read-only identity from explicit cache/workspace creation.
- [ ] Detect bundled launcher candidates and statically determine Ren'Py major/minor version.
- [ ] Reject Ren'Py 7, absent loose source, ambiguous launcher, path escape, and managed-SDK fallback in strict mode.
- [ ] Surface the Task 0-selected backend and its disposable capability probe through a fakeable interface.
- [ ] Enforce the exact tested Ren'Py 8 version by default and make unsupported-version opt-in explicit and traceable.
- [ ] Return structured support tier and refusal reasons.

**Gate:** preflight is read-only, deterministic, and does not execute game code.

### Task 3: Implement private named profiles and leases

**Files:**

- Create: `src/renforge/play/profiles.py`
- Modify: `src/renforge/util/files.py`
- Modify: `src/renforge/policy.py`
- Add wrapper in: `src/renforge/tool_registration/lifecycle.py` or a new `play.py` registration module
- Create: `tests/test_play_profiles.py`
- Modify: `tests/test_policy.py`

**Steps:**

- [ ] Write failing path-safety, symlink, collision, lock, and interrupted-write tests against the external state root.
- [ ] Create schema-versioned profile metadata with atomic private writes.
- [ ] Implement create/list/inspect without reading save contents; do not add reset.
- [ ] Implement the durable session state machine, one-live-session project lock, exclusive profile lease, and fixed lock ordering.
- [ ] Record PID plus process-start identity; never trust PID alone during recovery.
- [ ] Refuse unsafe roots and active/unverifiable lease reuse; quarantine uncertain state as `recovery-required`.

**Gate:** profile tests demonstrate that no administration path can target the project root, home, another profile, or a symlinked path, and concurrent launches cannot both acquire ownership.

### Task 4: Productize the selected Linux isolation backend

**Files:**

- Create: `src/renforge/play/sandbox.py`
- Create: `src/renforge/play/runtime_paths.py`
- Create: `tests/test_play_sandbox.py`
- Create: `tests/fixtures/play_sandbox_project/` with tiny non-Ren'Py sentinel files

**Steps:**

- [ ] Write failing command-plan tests for the Task 0-selected overlay or full-copy layout, save binds, private home/XDG/temp, guest-publication bind, and minimum system/Xvfb mounts.
- [ ] Model host, sandbox, and logical project paths explicitly; do not compare strings from different namespaces without translation.
- [ ] Implement a disposable mount/write probe that proves lower-layer writes land in upper and `game/saves` writes land in the profile bind.
- [ ] Start from an empty mount namespace and omit normal saves, unrelated home/workspace paths, and trusted RenForge control state.
- [ ] Reject symlinked/FIFO/non-directory bind targets and prove the guest cannot enumerate or read omitted canaries.
- [ ] Keep network namespace shared for the TCP bridge.
- [ ] Add bounded diagnostics for missing Bubblewrap, unsupported backend, insufficient copy space, mount denial, and Xvfb failures.

**Gate:** the automated hostile-process suite from Gate A passes against production command construction.

### Task 5: Add bundled-runtime launch and complete process ownership

**Files:**

- Modify: `src/renforge/bridge/launcher.py`
- Modify: `src/renforge/launch_env.py`
- Create: `src/renforge/play/launcher.py`
- Modify: `src/renforge/tools/live.py`
- Modify: `src/renforge/tool_registration/lifecycle.py`
- Create: `tests/test_play_launcher.py`
- Modify: `tests/test_bridge_launcher.py`
- Modify: `tests/test_live_stop.py`

**Steps:**

- [ ] Write failing tests for game-specific `.sh` launch command shape and native `--savedir` placement.
- [ ] Build an allowlisted environment; assert unrelated sentinel secrets are absent.
- [ ] Allocate/persist the session before background launch and make starting/failure queryable by `session_id`.
- [ ] Launch through the selected sandbox plan with Xvfb, dummy audio, a dedicated process group, Bubblewrap PID namespace/reaper, and `--die-with-parent`.
- [ ] Inject bridge/session artifacts into the disposable runtime layer, not host `game/`.
- [ ] Make strict launch bypass dashboard delegation and refuse reuse/attachment of legacy, external, or already-live sessions.
- [ ] Make teardown kill/reap the owned namespace and process group before releasing lease or deleting runtime data; there is no live reattachment path.
- [ ] Fingerprint tracked `.rpy` source at launch and taint/refuse further action or successful completion after an in-session host edit.
- [ ] Preserve legacy launch behavior behind `isolation="legacy"` without representing it as strict.

**Gate:** fake-process tests prove command/environment/ownership, and sandbox sentinel tests prove no host project injection.

### Task 6: Relocate control metadata and enforce live attestation

**Files:**

- Modify: `src/renforge/bridge/control.py`
- Modify: `src/renforge/bridge/artifacts.py`
- Modify: `src/renforge/bridge/bridge.rpy`
- Modify: `src/renforge/bridge/client.py`
- Modify: `src/renforge/bridge/launcher.py`
- Create: `src/renforge/play/attestation.py`
- Create: `tests/test_play_attestation.py`
- Modify: `tests/test_bridge_resources.py`
- Modify: `tests/test_bridge_launcher.py`

**Steps:**

- [ ] Parameterize bridge/control/artifact metadata roots while retaining ownership and no-follow checks; expose only the fresh guest-publication directory to the sandbox.
- [ ] Add a bridge attestation handler with bounded, JSON-safe path/location reporting.
- [ ] Translate sandbox paths to host ownership roots before validation.
- [ ] Fail closed on escaped/unobservable save locations including `config.extra_savedirs`, wrong gamedir, wrong/tested engine version, or writable lower-layer proof.
- [ ] Ensure failed attestation tears down the process and leaves normal sentinels untouched.
- [ ] Never report `ready` before both host canary verification and semantic attestation pass.
- [ ] Add crash-recovery tests for dead, live-owned, and unverifiable leases, preserving diagnostics before cleanup.

**Gate:** attestation corroborates the host isolation proof, and guest code cannot read or change trusted lease/launch/trace metadata.

### Task 7: Implement one atomic bridge observation

**Files:**

- Modify: `src/renforge/bridge/bridge.rpy`
- Modify: `src/renforge/bridge/client.py`
- Create: `src/renforge/play/observation.py`
- Modify: `src/renforge/state_compact.py` if shared bounded serialization is needed
- Modify: `tests/test_bridge_runtime.py`
- Create: `tests/test_play_observation.py`

**Steps:**

- [ ] Write fake-runtime tests proving screenshot/state/screens/menu/focusables share one handler call.
- [ ] Extract active-screen enumeration from the editor-only implementation into bridge-safe runtime code.
- [ ] Add outer-interaction and restart-generation callbacks plus a bounded semantic signature.
- [ ] Read generations before and after semantic/PNG capture; mark/retry inconsistent transactions within a strict bound.
- [ ] Include safe element identity/stability metadata and explicit completeness/output-size limits.
- [ ] Add event-ring base cursor and dropped-event reporting.
- [ ] Prove an animated-frame fixture changes frame hash without changing semantic identity, while successive identical dialogue interactions change generations.
- [ ] Keep bridge work bounded; polling/stability waiting remains host-side.

**Gate:** atomic observation tests cover dialogue, standard menu, custom imagebutton, transition, animation, truncation, and event loss.

### Task 8: Expose multimodal `renforge_observe`

**Files:**

- Modify: `src/renforge/tool_registration/wrappers.py`
- Modify or create: `src/renforge/tool_registration/inspection.py` / `play.py`
- Modify: `src/renforge/tools/live.py`
- Modify: `src/renforge/image_ops.py`
- Create: `tests/test_play_observe_tool.py`
- Modify: `tests/test_server.py`

**Steps:**

- [ ] Add a helper returning ordered text plus image MCP content blocks without backend stringification.
- [ ] Make `session_id` required and reject stopped, failed, legacy, or successor-session ambiguity.
- [ ] Implement host-side change-then-settle polling for `after_observation_id`, with bounded timeouts.
- [ ] Return the latest observation on no-change/instability and mark `changed`, `stable`, `consistent`, and the reason.
- [ ] Ensure trace storage receives exact raw PNG bytes and manifest before MCP output encoding.
- [ ] Test content ordering, native dimensions, MIME type, image hash, oversized/truncated manifests, and error envelopes across supported MCP backends.

**Gate:** an MCP client receives readable JSON and the corresponding raw image in one tool result.

### Task 9: Implement guarded minimal actions

**Files:**

- Modify: `src/renforge/bridge/bridge.rpy`
- Modify: `src/renforge/bridge/client.py`
- Modify or create: `src/renforge/tool_registration/interaction.py` / `play.py`
- Modify: `src/renforge/tools/live.py`
- Create: `tests/test_play_actions.py`
- Modify: `tests/test_bridge_runtime.py`
- Modify: `tests/test_policy.py`

**Steps:**

- [ ] Write stale-session, stale-observation, successor-session, missing-element, disabled, advisory-occlusion, and bad-coordinate tests.
- [ ] Recompute and verify observation identity immediately before dispatch in the same main-thread transaction.
- [ ] Reuse existing pointer/click and dismiss seams; do not create a second synthetic-input implementation.
- [ ] Correlate accepted actions with bridge business events and trace IDs.
- [ ] Never automatically retry or fall back from element click to coordinate click.
- [ ] Return `next="observe"` with a bounded immediate-effect summary.

**Gate:** only click-element, logical click-at, and advance action types validate; every other type fails before runtime dispatch.

### Task 10: Implement the minimal evidence journal and checkpoint seam

**Files:**

- Create: `src/renforge/play/trace.py`
- Create: `src/renforge/play/checkpoints.py`
- Modify: `src/renforge/tools/live.py`
- Add wrapper in: `src/renforge/tool_registration/play.py`
- Modify: `src/renforge/activity_log.py` only to link, not duplicate, play events
- Create: `tests/test_play_trace.py`
- Create: `tests/test_play_checkpoints.py`

**Steps:**

- [ ] Write append, fsync/atomic metadata, truncated-tail recovery, deduplication, quota, and concurrent-writer tests.
- [ ] Create a trace automatically on strict recorded launch.
- [ ] Store exact pre-action observations and content-addressed raw frames.
- [ ] Journal requested/rejected/dispatched actions and exceptions.
- [ ] Add required session/observation guards and checkpoint metadata around existing isolated save/load commands; support one named MVP checkpoint.
- [ ] Add a read-only trace status view and one `renforge_play_finish` transition with `complete`, `needs_intervention`, or `failed` status.
- [ ] Refuse finish without a final observation and explicit agent reason; never allow a finished outcome to be rewritten.
- [ ] Preserve unfinished trace resumability across session restart with the same project/profile.

**Gate:** a simulated process interruption leaves a recoverable JSONL prefix and never produces a falsely finished trace.

### Task 11: Build the synthetic real-engine acceptance fixture

**Files:**

- Create: `examples/minimal_play_game/game/script.rpy`
- Create: `examples/minimal_play_game/game/screens.rpy`
- Create: `examples/minimal_play_game/game/options.rpy`
- Create: `tests/test_integration_minimal_play.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `scripts/smoke_renpy_launch.py` or create a focused minimal-play runner

**Fixture behavior:**

- existing lower-layer `game/saves` sentinel;
- external normal-save canaries that game Python tries to enumerate/read/write;
- persistent and MultiPersistent variables written during earliest initialization and during play;
- configured `extra_savedirs` escape attempt;
- custom relative file write attempt beneath `game/`;
- dialogue and standard menu;
- custom imagebutton decision;
- continuously animated background;
- two successive interactions with identical visible dialogue;
- one click/advance-only completion label;
- one unsupported input screen on a non-acceptance branch.

**Steps:**

- [ ] Write the opt-in end-to-end test before production integration.
- [ ] Launch only through strict profile mode and assert attestation.
- [ ] Drive one deterministic complete fixture route via observe/act, always using change-then-settle observations.
- [ ] Assert every user-visible choice/control has native-PNG pixel bounds, matching semantic text/role where available, and a valid in-frame click point.
- [ ] Stop/relaunch the same profile and load a checkpoint.
- [ ] Verify the guest could neither enumerate/read nor alter external normal-save canaries, and verify lower/project sentinels byte-for-byte.
- [ ] Verify profile separation with a second empty profile.
- [ ] Kill the MCP owner mid-run after a child forks/setsid; verify fail-stop termination, diagnostic preservation, and safe lease/runtime recovery.
- [ ] Verify strict launch refuses dashboard/external/legacy bridge reuse, and stale commands from session A fail after session B starts.
- [ ] Store only bounded synthetic diagnostics as CI artifacts; never publish bridge tokens.

**Gate B:** the synthetic route passes under the exact pinned Ren'Py 8 engine, Xvfb, and dummy audio; all isolation, visual-coherence, stale-action, and recovery assertions pass before a real game is launched.

### Task 12: Document workflow and perform local real-game proof

**Files:**

- Modify: `README.md`
- Modify: `docs/MCP.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/POLICY.md`
- Modify: `CHANGELOG.md`
- Create local-only runner/report output under the external RenForge state root; do not add game files

**Steps:**

- [ ] Document the user-owned unpacked-source prerequisite and strict non-goals.
- [ ] Document profile lifecycle, storage quotas, fail-stop behavior, and recovery-required handling.
- [ ] Mark legacy temporary savedir behavior as not equivalent to strict isolation.
- [ ] Document the agent observe/act/checkpoint/finish loop.
- [ ] Select one user-prepared Ren'Py 8 development project derived from `/home/aaron/Spicey/hgame/` whose complete route needs only click/advance; keep its contents outside this repository.
- [ ] Hash the project tree and known normal-save sentinels before launch.
- [ ] Complete one agent-driven route, including a stop/relaunch/checkpoint restore.
- [ ] Verify the trace explains every action from its native frame and pre-action manifest and that choices were visible where decisions occurred.
- [ ] Re-hash project and normal-save sentinels and confirm only explicitly authored source changed; all RenForge runtime/play state remains outside the project.
- [ ] Record engine version, route limitations, unsupported interactions, and exact evidence locations in a local acceptance report.

**Final gate:** all MVP 1 exit criteria below pass. Only then may MVP 2 route-schema work begin.

---

## 10. Verification commands

Focused commands are refined as files land, but the final implementation must provide and pass these layers:

```bash
pytest -q tests/test_play_isolation_spike.py
pytest -q tests/test_play_contracts.py tests/test_play_preflight.py tests/test_play_profiles.py
pytest -q tests/test_play_sandbox.py tests/test_play_launcher.py tests/test_play_attestation.py
pytest -q tests/test_play_observation.py tests/test_play_observe_tool.py tests/test_play_actions.py
pytest -q tests/test_play_trace.py tests/test_play_checkpoints.py
pytest -q tests/test_bridge_launcher.py tests/test_bridge_runtime.py tests/test_live_stop.py tests/test_policy.py
RENFORGE_SDK_TESTS=1 xvfb-run -a pytest -q tests/test_integration_minimal_play.py
pytest -q
```

---

## 11. MVP 1 Definition of Done

### Isolation

- [ ] Strict launch uses the bundled Ren'Py 8 runtime and native `--savedir`.
- [ ] Strict launch cannot enumerate or read normal-play saves or trusted RenForge control state.
- [ ] Project lower files and external normal-save sentinels remain byte-identical; project-path runtime writes exist only in disposable state.
- [ ] Game-local saves, primary saves, persistent data, and MultiPersistent are profile-owned; home/XDG/temp are session-private and disposable.
- [ ] Live attestation reports and validates every relevant location before readiness.
- [ ] A second empty profile cannot observe the first profile's state.
- [ ] Stop and owner crash do not leave an owned game process or unlock a live/unverifiable profile.

### Observation and action

- [ ] One MCP call returns coherent JSON plus its exact raw screenshot.
- [ ] Active standard/custom screens and visible focusables are reported with enough text/role/bounds and completeness metadata to explain each fixture decision.
- [ ] Animated pixels do not invalidate a semantically stable control set.
- [ ] Consecutive identical-looking interactions and interaction restarts still receive distinct guarded identities.
- [ ] Stale actions are rejected before click/advance dispatch.
- [ ] Only click-element, logical click-at, and advance are accepted.

### Trace and recovery

- [ ] Every dispatched action references a retained pre-action observation.
- [ ] Frames are content-addressed/deduplicated and quota omissions are explicit.
- [ ] A named checkpoint survives stop/relaunch of the same isolated profile.
- [ ] Interrupted journals recover to the last complete event without false completion.
- [ ] The single finish marker requires an agent reason and final observation and cannot be rewritten.

### End to end

- [ ] The synthetic real-engine route passes in CI.
- [ ] One local real Ren'Py 8 click/advance route completes from start to agent-declared end.
- [ ] The agent can explain each decision using the stored frame and action manifest.
- [ ] The running game changes no host project file; any host source change was an explicit agent edit outside the runtime.
- [ ] No game data, extracted source, save, persistent file, or bridge credential is committed or uploaded.

---

## 12. Known risks and deliberate responses

| Risk | MVP 1 response |
| --- | --- |
| Bubblewrap overlay unavailable on a host | Use only the size-preflighted full-copy fallback if Gate A proves the same boundary; otherwise fail preflight. |
| Bundled launcher needs unexpected system paths | Add the smallest explicit read-only mount and regression test; never expose all of home. |
| Game uses a hard-coded absolute save path | Hide normal home paths in the empty namespace; attestation covers engine locations, while hostile custom-write tests cover common escapes. |
| Animation changes every screenshot | Guard on observation identity and semantic generations/signature; retain exact frame hash separately. |
| Image-only control lacks a durable label | Return its native-frame bounds and screen/role hints; allow guarded coordinate click. |
| Current screen uses unsupported input | Checkpoint and mark `needs_intervention`; do not guess. |
| MCP dies with game alive | `--die-with-parent`, PID namespace, and group ownership make the session fail-stop; a new process recovers durable state without live reattachment. |
| Trace storage grows without bound | Content addressing, explicit quotas, and omission events. |
| Unpacked source is incomplete or inaccurate | Treat source fidelity as a user prerequisite; runtime observation remains authoritative. |
| Strict and legacy launches are confused | Return `isolation` and attestation in every launch/status response; strict tools refuse legacy sessions. |

---

## 13. Future MVP 2 seam

MVP 2 consumes only stable MVP 1 contracts:

- observation classification, label, screens, menu choices, and elements;
- observation identity, UI generations, and semantic signature;
- guarded action transaction;
- checkpoints;
- durable trace events.

It adds:

1. a versioned route YAML schema;
2. unambiguous step matching by label/screen/text/element/occurrence;
3. `choose_text`, `element_id`, coordinates, and `agent` actions;
4. expected-next-state and timeout assertions;
5. divergence handling and resume cursor;
6. repeatable before/after walkthrough-mod evidence;
7. adapter extraction only after repeated real-game exceptions demonstrate a stable need.

MVP 2 must not weaken MVP 1 isolation, bypass guarded actions, or create a second observation/trace implementation.
