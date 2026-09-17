# RenForge minimal play — unified final plan

**Status:** consolidated plan for user review; implementation is not authorized by this document alone. This is the single proposed implementation plan, replacing the August 31 plan and September 16 simplification proposal. Those documents remain historical references, not additional requirements.

**Baseline:** `main` at `e4fa61f` merged by `c7afdd4`; 124 focused bridge/runner/cloud-script tests passed after that merge. Gates A and B below remain open. No strict-play implementation or real-game route has been accepted.

## Outcome and limits

An agent completes one click/advance-compatible route through a user-prepared, loose-source Ren'Py development project. It sees coherent native images and actionable semantics, uses guarded inputs, saves a checkpoint, stops and relaunches into the same isolated profile, and leaves a durable decision trace. The running game does not alter host project files or access normal-play saves through the mounted filesystem.

The acceptance target remains `/home/aaron/Spicey/hgame/MyPigPrincess-0.3.0-pc`, reported as Ren'Py **8.2.0.24012702**. Revalidate these facts before acceptance. The user-provided name-defaulting source hook must be present before baseline hashing; RenForge does not add text-input support to get past that prompt. Completion means one agent-declared ending, not exhaustive coverage or automatic ending detection.

Support is limited to Linux and the exact tested engine/runtime. No unsupported-version opt-in for the initial milestone. Source completeness/fidelity remains the user's responsibility. No archive extraction, SDK fallback at strict launch, live editing, text entry, keyboard automation, drag/drop, minigames, route schema, automatic replay, profile import/reset, or cross-platform strict runtime.

This is filesystem and owned-process isolation, not containment of malicious game code against the shared network or every same-user host service. The TCP bridge requires a shared network namespace. Tokens authenticate clients but are visible to game Python; guest attestation is corroboration, never the security proof.

## Decisions that simplify delivery

| Decision | Initial implementation |
| --- | --- |
| Filesystem backend | One backend: host-side `fuse-overlayfs` plus an empty allowlisted Bubblewrap namespace, subject to Gate A on local and CI hosts. No automatic fallback. |
| If FUSE cannot run in CI | Stop at environment proof. Select and prove full-copy as the single replacement backend, or choose an appropriate runner. Record the decision here before production work; do not implement two backends speculatively. |
| Display | One fresh, session-owned Xvfb server, locally and in CI. Cage/weston selection is deferred; the earlier preference is superseded in this proposed plan. |
| Operations | One active strict session per canonical project; serialize session operations and use one journal writer. Observation waits remain bounded and cancellable so stop is not starved. |
| Evidence | Every strict session records a trace. Reject `record_trace=false`. |
| Delivery | Grow one synthetic fixture from environment proof through final acceptance. Introduce public tools only alongside working behavior and tests. |

Keep existing legacy launch/editor behavior and existing Ren'Py 8.5.3 CI coverage. Neither counts as proof of strict-play support. Reuse bridge input and serialization seams where proven; do not reuse legacy lifecycle or completion claims without testing their strict semantics.

## Filesystem, ownership, and launch contract

Use an external user-owned private state root, outside the project, with no-follow validation and private directories/files. Keep project identity derived from the canonical project path; record source/runtime fingerprints separately. Store profiles, leases, session metadata, diagnostics, and traces there. Never create project-local `.renforge` as part of strict preflight or launch.

Profiles contain only designated primary saves, game-local saves, and MultiPersistent state. Session runtime, home, XDG directories, cache, temp, publication, and display data are disposable. Bind profile game saves over `game/saves`; invoke the bundled launcher with native `--savedir`. Set version-proven save/MultiPersistent environment variables before engine initialization. Never promote runtime writes back to source.

Build an allowlisted environment and an empty mount namespace. Mount required system paths **read-only**; do not bind host `/`, home, workspace, normal saves, other profiles, or trusted RenForge metadata. Expose only the session's display endpoint and minimal devices/private proc. Mask project-local normal-save/control artifacts before guest execution; reject unsafe or unowned bridge artifacts rather than overwrite them. Validate bind sources/targets, reject FIFOs and escaping symlinks, and close unintended inherited file descriptors. Arbitrary sensitive files bundled by the user inside the project remain part of supplied game content; do not claim to identify them automatically.

Expose only a fresh `/run/renforge` publication directory to the guest. Trusted launch/lease/trace files stay outside the guest. Parse publication as untrusted, bounded data with ownership/no-follow checks. Private publication is a discovery channel, not proof that the guest is trustworthy.

Preflight is read-only with respect to project/profile state: it may run a disposable capability probe in private temporary storage and must clean it. It statically checks loose source, one selected bundled launcher, engine version, contained runtime paths, source/artifact conflicts, state-root safety, and backend availability without executing game code. Refuse ambiguous launchers and unsupported versions. Do not refactor all legacy `RenpyProject` users just to obtain a read-only strict project descriptor.

Allocate durable project/profile/session/trace identities before asynchronous launch. Lock in fixed project → profile → session order. Lifecycle is `prepared → starting → ready → stopping → stopped`, with `failed` and `recovery-required` outcomes. Status is queryable by session ID even if startup fails. Refuse legacy/external process attachment and conflicting launches, including through legacy entry points.

One explicit owner/supervisor must manage the game namespace, Xvfb, and host FUSE helper/mount. Prove owner death, not just killing the Bubblewrap leader. Teardown stops/reaps processes, verifies the mount is detached, preserves bounded diagnostics, then releases leases and deletes disposable state. Do not recursively delete a mounted runtime. If ownership/death/unmount cannot be proved, quarantine and refuse profile reuse. Recovery compares boot/process-start identity as well as PID; never kill a numeric PID alone or reattach a prior live game.

Before `ready`, validate live engine version, basedir/gamedir, every recognized load/save location, extra savedirs, MultiPersistent root, home/XDG/temp, and publication path against an explicit translated path allowlist. Unobservable save locations fail launch. Pair this with host-side mount/write canaries proving the lower source stays unchanged. Reject failed attestation and tear down fully.

Fingerprint all loose `.rpy` paths and bytes, including additions/deletions, plus launcher/runtime identity. Check again after startup, before each mutation, and before successful finish. A detected change taints the session and blocks actions/success. This is detection at operation boundaries, not protection against a deliberately racing host editor; edits require stop/relaunch. Changed-source continuation starts a new trace and does not silently load an incompatible checkpoint.

## Public workflow and contracts

Every response identifies project/profile/session/trace where applicable. Use `project_path` only for preflight, profile administration, and initial launch; live strict operations require `session_id`. Tool schema/risk metadata and public contract snapshots change with implementation.

| Surface | Strict behavior |
| --- | --- |
| `renforge_play_preflight(project_path, launcher_path="")` | Static project checks plus disposable capability probe; structured refusals and source-completeness `unverified`. |
| `renforge_profile_create/list/inspect` | Named empty profiles, bounded metadata, lease inspection; no save payload reads or reset/import. |
| `renforge_launch` | Require strict isolation, bundled runtime, profile, editor off, mandatory trace; optional unfinished trace ID for clean stop/restart continuation. Reject arbitrary env/savedir overrides. |
| `renforge_launch_status` | Add session-ID lookup for strict starting/ready/failed/stopped state, selected backend, version, fingerprints, and attestation. |
| `renforge_observe(session_id, after_observation_id="", settle_ms=400, timeout=10)` | Ordered JSON manifest plus corresponding native raw PNG; bounded change-then-settle wait. |
| `renforge_act(session_id, observation_id, action)` | Only element click, logical-coordinate click, or dialogue advance; immediate validated dispatch status and correlation ID, then observe. |
| Existing save/load and stop tools | Add strict session-bound paths; save/load also require observation guards and journal evidence. No project-only fallback for a strict session. |
| `renforge_play_finish(session_id, observation_id, status, reason)` | Close once with `complete`, `needs_intervention`, or `failed`; success requires fresh coherent stable evidence, failure/intervention may reference last available evidence or explicitly report none. |
| Trace inspection | Bounded read-only status/metadata surface; no new trace lifecycle tool family. |

Reject legacy access to strict sessions at shared session resolution and enforce a strict bridge command allowlist. Include advance/control/input, choice selection, eval/store mutation, editor writes, and Autopilot. Autopilot must fail before its stop/relaunch side effects. For MVP, legacy live reads also refuse strict sessions; use strict status/observe. Strict rejection must precede any project-local cache creation or other legacy side effect. Maintenance/shutdown stop paths retain ownership checks.

### Coherent observation and immutable evidence

Each successful coherent capture receives a fresh opaque `observation_id`; its immutable manifest references `frame_id = SHA-256(exact PNG bytes)`. Separate capture identity from validity: the stored guard contains session, interaction/restart generations, mutation epoch, semantic signature, and element table. A new capture token alone is not a semantic change.

The manifest includes native/logical/window dimensions and their coordinate mapping, classification, label, dialogue, screens/modal ownership, menu choices, visible controls with text/role/bounds/enabled state, generation/signature fields, event cursor/loss, and completeness flags. Normalize bounded semantic values; never invoke arbitrary action objects merely to label them. Missing semantics are explicit. Use frame-local element IDs resolving only through their capture's table; durable cross-observation widget identity is deferred. Validate the captured target against the current normalized element table before dispatch. Coordinate clicks remain available when the image is coherent but semantics are incomplete.

Capture semantics and PNG in one bounded engine transaction at a **proven render-coherent point**. Before/after generation checks are necessary but not sufficient: the screenshot may still be the prior rendered frame. Prove the actual seam on 8.2.0 with fixture pixel markers matching semantic state across immediate dialogue/menu/load transitions. Choose the engine hook based on that experiment, not an assumed callback contract.

Exhausted consistency retries return `observation_unavailable`, diagnostic metadata/image if available, and **no actionable observation ID**. A coherent but unsettled capture is returned with `stable=false`; actions require a coherent stable capture. Define the change/settle tuple as interaction generation, restart generation, and semantic signature only: exclude mutation epoch, capture ID, and PNG hash. Without `after_observation_id`, settle on that unchanged tuple; with it, require that tuple to change first. The mutation epoch only invalidates authorization; incrementing it never implies `changed=true`. Animated pixels alone neither invalidate a guard nor satisfy `changed`. Identical-looking successive interactions must change generations. Waits occur host-side, never as a blocking engine sleep.

### Guarded actions and queued input

Before dispatch, validate session/source identity, retain evidence durably, then recompute and verify the guard on the engine thread. Reject missing/disabled elements, invalid coordinates, consumed/expired/foreign observations, and unsupported action types. Occlusion is advisory unless the target engine supplies a proven check. Do not retry or automatically fall back to coordinate clicking.

Acceptance advances a session-wide monotonic mutation epoch outside rollback/save state and invalidates **all** earlier capture authorizations, including separate observations with identical semantics. A pending-input barrier prevents another mutation or actionable observation until the engine proves it processed the input. Host serialization and merely posting an event do not prove this. Define and test the engine acknowledgment seam before shipping actions. A processed click can be a no-op; `changed=false` does not mean undispatched. Processing timeout/lost acknowledgment is `outcome_unknown`, not permission to retry.

Observe after acknowledgment obtains a new guard; observe while pending can provide diagnostics but cannot re-arm input. Checkpoints and successful finish refuse pending operations. Ownership-checked stop/teardown always remains available despite pending input, source taint, or evidence failure: it can cancel waits and never requires successful journaling or input acknowledgment. Preserve an interrupted operation as unknown rather than waiting indefinitely to stop.

## Trace, checkpoint, and failure semantics

Use schema-versioned `trace.json`, append-only `events.jsonl`, immutable `observations/<id>.json`, content-addressed `frames/<hash>.png`, and checkpoint metadata. Omit speculative MVP 2 fields. Every event has sequence, identities, correlation where applicable, and explicit outcome. Record launch/attestation, observations, requested/rejected/dispatched/processed-or-unknown mutations, checkpoint transitions, runtime exceptions, clean session stop, and final outcome.

Before any action or checkpoint save/load, ensure the exact pre-operation manifest/frame and request record are durable. Use file fsync, atomic rename, parent-directory fsync for newly published files, and journal fsync in defined order. Store outcomes afterward. Quotas cover both evidence and journal; reserve bounded outcome/failure capacity, but still handle physical disk exhaustion. Failure before dispatch returns `evidence_unavailable` without input. Never discard required evidence to keep playing. Internal polling samples may be omitted; returned actionable captures must remain available under a bounded documented retention/expiration policy.

Durability cannot make engine input and host journal atomic. An interrupted request without a durable verified outcome remains unknown permanently; a fresh frame does not rewrite history. Fail-stop the session. If storage permits, finish that trace as `failed`; otherwise leave it unfinished/recovery-required. After recovery, a new trace may start from a verified checkpoint with predecessor/uncertainty metadata. It cannot claim the interrupted trace completed. Final clean-route acceptance requires a trace with no unknown operations.

A checkpoint is one named Ren'Py save slot plus metadata binding it to project/profile, source/runtime fingerprints, creation observation, and trace cursor. Record `checkpoint.saved` only after engine save completion and verification that the resulting slot is usable and durable. An interrupted overwrite invalidates checkpoint usability unless the prior slot can be proved intact. Starting/failed checkpoint writes never become resumable through metadata alone.

Loading records `load.requested`, invalidates all prior observations via the monotonic mutation epoch, and waits for verified restored state and a new render-coherent observation before `checkpoint.loaded`. Existing `load.completed` emitted when scheduling `renpy.load` is not sufficient. Restored label alone is not sufficient either: the fixture must assert a checkpoint-specific state marker and decision state. Preserve the monotonic epoch outside restored game state.

Clean stop leaves an unfinished trace resumable on the same project/profile/fingerprints in a new session, with explicit checkpoint load before continuation. New session IDs invalidate all prior live commands. A `complete` finish requires fresh retained coherent stable final evidence, reason, no pending/unknown mutations, intact required records, and no source taint. `failed`/`needs_intervention` may use the last coherent capture or an explicitly absent capture with a diagnostic reason when startup/runtime failure prevents observation; unknown mutations remain explicitly recorded and require a failed outcome. After finish, forbid further play mutations. Stop updates session metadata without reopening the trace. Failure to persist finish is not successful completion.

## Implementation sequence and acceptance gates

The steps below replace the old task/wave ordering. Implement focused failing tests for behavioral contracts, then the smallest passing change. Serialize edits to `bridge.rpy`, `tools/live.py`, and tool registration/snapshots. Worker count and model are not architecture requirements.

### Stage 0 — prove the environment and capture/input seams

**Primary files:** reviewed portions of `scripts/spike_play_isolation.py` and `tests/test_play_isolation_spike.py` from `35ce6fe`; new synthetic fixture and bootstrap/capability helpers; `.github/workflows/ci.yml`.

- [ ] Review/integrate the spike separately from the main merge. Correct its writable system binds. Historical 9/9 results are not current Gate A evidence.
- [ ] Provision an official runtime matching full version 8.2.0.24012702; record download provenance and pinned checksum, verify static/live version. Assemble a disposable development bundle around repository-owned synthetic code. No real target scripts/launcher in this stage.
- [ ] Prove the single backend on the intended local and CI hosts, including sufficient storage, unsafe symlink/FIFO/bind failures, private environment/FDs, read-only system mounts, hidden normal-save/control trees, and write landing in each intended destination.
- [ ] Prove a session-owned Xvfb renders the synthetic bundle. Use bounded fork/setsid/double-fork descendants; do not run an unbounded fork bomb or equate PID isolation with resource quotas.
- [ ] Kill the actual owner and independently fail game/display/FUSE startup. Verify no owned game/display/helper remains, no live mount is recursively deleted, and ambiguous teardown preserves quarantine/leases.
- [ ] Probe render-coherent capture and queued-input acknowledgment on the pinned engine before locking observe/action schemas. Prototype through fixture code if production bridge integration is not yet ready.

**Gate A:** repeatable host filesystem/process/display boundary proof on the chosen supported configuration. A real-game launch remains prohibited. Missing runtime/backend prerequisites block this stage rather than silently changing support claims. Engine-specific save semantics remain Gate B work.

### Stage 1 — strict launch, readiness, and stop

**Primary files:** new `play/contracts.py`, `preflight.py`, `profiles.py`, `sandbox.py`, `launcher.py`, `attestation.py`, minimal `trace.py`; narrow integration in `bridge/control.py`, `artifacts.py`, `launcher.py`, `bridge.rpy`, and lifecycle registration.

- [ ] Implement private profile metadata, durable identities/leases, lifecycle state machine, bounded diagnostics, and single session operation ownership.
- [ ] Implement read-only project description and strict preflight without invoking the cache-creating legacy project constructor.
- [ ] Productize Gate A's exact mount/environment/process construction and guest-only bridge injection/publication.
- [ ] Implement strict launch/status/stop with full readiness attestation and journal foundation; reject dashboard delegation, external attachment, and legacy conflicts.
- [ ] Add earliest-init persistent/MultiPersistent/save canaries. Test `extra_savedirs` escape as a separate rejected-launch fixture, not inside the successful route.
- [ ] Exercise source taint, startup failures at each resource acquisition, second-profile separation, owner death, lease recovery, and repeat launch/stop.

**Exit:** the actual bundled synthetic game reaches attested ready and stops without modifying lower/normal-save canaries. Production construction passes Gate A tests; unverified ownership prevents reuse.

### Stage 2 — complete the synthetic observe/action/restart loop

**Primary files:** `play/observation.py`, `trace.py`, `checkpoints.py`; existing bridge/client/input seams; strict wrappers in `tool_registration/play.py` and `tools/live.py`; `tests/test_integration_minimal_play.py` plus focused contract tests.

- [ ] Implement proven capture and input acknowledgment seams, immutable observation records, semantic change/settle logic, mutation epochs, and strict command dispatch restrictions.
- [ ] Verify exact JSON/PNG pairing through a real MCP server/client transport; decoded PNG bytes must match frame hash and native dimensions.
- [ ] Extend the fixture with dialogue, menus, custom imagebuttons, animation, identical-looking consecutive interactions, scaled-coordinate checks, unsupported-input branch, and render/state markers.
- [ ] Cover inconsistent/unstable captures, stale and duplicate actions from different same-state observations, no-op input, pending barriers, truncation/event loss, storage quotas/failure, and crash windows around dispatch.
- [ ] Verify checkpoint completion, stop/relaunch/load, post-load epochs, persistent state, second-profile separation, stale session A calls after session B, and strict rejection of every legacy live surface before side effects.
- [ ] Complete a deterministic synthetic route using only the public workflow and retain all decision/final evidence. Kill the MCP owner through the real transport mid-run and exercise honest unknown-outcome recovery separately.

**Gate B:** the full synthetic route and negative/recovery cases pass on the pinned runtime under production isolation, with no normal-save access, no lower mutation, coherent decisions, durable evidence, and verified restore. Only after Gate A and Gate B pass may the real game run.

### Stage 3 — local real route and documentation

**Primary files:** `README.md`, `docs/MCP.md`, `docs/ARCHITECTURE.md`, `docs/POLICY.md`, `CHANGELOG.md`; private report under the external state root.

- [ ] Revalidate target source/runtime, name-defaulting hook, compatibility limitations, and a candidate click/advance route. Hash source/project tree and known normal-save sentinels before launch.
- [ ] Start an empty profile, complete one agent-driven route, and include a clean checkpoint/stop/relaunch/verified-load cycle in the same trace.
- [ ] Retain a native frame and matching manifest for every dispatched decision, plus final observation/reason. Unsupported interactions produce `needs_intervention`, not a false route success.
- [ ] Verify project/save hashes after stop, inspect trace for unknowns/missing evidence, and record engine/backend/fingerprints/checkpoint/route limitations and private evidence paths.
- [ ] Document strict workflow and refusal/recovery behavior. State clearly that legacy temporary savedirs are not strict isolation. Do not publish target files, saves, source, frames, or bridge credentials.

**Final acceptance:** both gates plus one clean real route, verified checkpoint recovery, unchanged host project/normal-save sentinels, and complete explainable decision evidence. MVP 2 remains deferred until this passes.

## CI and validation policy

Keep the existing 8.5.3 job; add a Linux strict-play job pinned to 8.2.0.24012702 and Python 3.12, with host-only unit/schema regression coverage on supported Python versions. Install the selected backend/Xvfb/runtime libraries explicitly. A setup SDK may construct the synthetic bundle; strict launch still uses that bundle's runtime.

The strict job owns its session displays and cannot borrow legacy `DISPLAY=:99`. Required-gate mode fails on missing prerequisites, capability denial, skipped acceptance cases, or zero collected gate tests. Local normal test runs may leave engine tests opt-in. Upload only bounded allowlisted synthetic diagnostics, sanitized manifests, and fixture frames; exclude tokens, publication/control directories, and profile payloads.

Run focused contracts per stage, existing affected bridge/lifecycle/policy/tool snapshot regressions, then required synthetic integration. At final integration run the full supported regression suite and packaging checks; frontend checks are required if frontend/build assets change. Record exact baseline failures on the current commit rather than blanket-excluding historical failures. No real game was launched and no gate was rerun merely by authoring this document.

## Review synthesis and repository handoff

Three independent reviewers read both predecessor plans and relevant code: a skeptical senior developer, an isolation/recovery reviewer, and a runtime/MCP/CI reviewer. Their blocking findings are incorporated here:

| Review finding | Resolution |
| --- | --- |
| Too many backends and late real-engine integration | One backend/display, fixture and runtime seams in Stage 0. |
| Conflicting preflight/gate promises | Disposable probes explicitly permitted; synthetic code only before both gates; real save semantics in Gate B. |
| Writable spike system binds and incomplete owner-death proof | Read-only system mounts; actual owner, Xvfb, FUSE and mount cleanup tested; bounded descendants. |
| Capture identity collision and counters insufficient for image coherence | Immutable per-capture IDs, semantic guard comparison, proven render seam, diagnostic-only inconsistent captures. |
| Different capture tokens can double-dispatch | Global mutation epoch plus engine acknowledgment barrier. |
| Quotas and crash ambiguity undermine evidence claims | Evidence-before-mutation; permanently recorded unknown outcomes fail the trace, never silently resume as complete. |
| Load completion is currently reported too early | Post-load state/render proof and epoch invalidation before checkpoint-loaded success. |
| Legacy tools bypass strict safety | Shared dispatch and bridge allowlists, including side-effect-free Autopilot rejection. |
| Stale worktrees and historical test claims | Recreate only needed worktrees from the merged branch; rerun evidence before claiming gates. |

Do not merge the spike or start implementation as part of reviewing this plan. Once this plan is accepted, Stage 0 is the next concrete work item; its provisioning/backend decisions must be resolved before production API work.
