# Minimal Play — proposed simplification and review fixes

> **Historical proposal — consolidated:** Use the [unified final plan](2026-09-16-renforge-minimal-play-final-plan.md) for review. It resolves the remaining issues found by three independent reviewers; this document is retained as review history, not a parallel execution plan.

**Status:** proposal for the next implementation revision, not an implemented capability or a replacement for the existing safety gates.
**Baseline:** `origin/main` at `e4fa61f`, merged into the plan branch by `c7afdd4` on 2026-09-16.
**Original plan:** [End-to-End Minimal Play](2026-08-31-renforge-end-to-end-minimal-play.md).

## What the main merge changes

The plan branch already contained the real-engine CI job, pinned to Ren'Py 8.5.3, and the narrowed Autopilot choice handling. This merge adds the 0.7.2 release metadata and the coordinator collection fix: results already drained by the periodic callback remain retrievable by request ID. The live runner now requests its own result and reports better timeout diagnostics.

These are useful foundations, but they do not prove strict-play isolation or the target game's 8.2.0.24012702 compatibility. Preserve the existing integration suite; add a distinct strict-play gate rather than repurposing its success as evidence for this milestone.

## Reduce the first milestone to one supported configuration

1. **One filesystem backend:** productize the existing host-side `fuse-overlayfs` + Bubblewrap spike first. Missing capabilities produce a clear preflight refusal. Defer full-copy fallback until it has its own Gate A evidence; do not advertise an untested fallback. Prove this backend on the intended CI runner before writing the public API. If CI cannot host it, explicitly choose and prove full-copy as the single initial backend instead of building both concurrently.
2. **One display backend:** use a fresh, session-owned Xvfb locally and in CI for MVP 1. Defer cage/weston selection and fallback. This is a proposed change to the August 31 display decision, not a claim that Wayland failed. Keep the selected backend in launch/trace metadata so later additions need no contract redesign.
3. **One fixture, grown incrementally:** create the synthetic project during Task 0, then extend it through launch, observation, actions, checkpoint recovery, and final acceptance. Do not wait until Task 11 to discover real-engine incompatibilities.
4. **One serialized session pipeline:** serialize observe, action, checkpoint, finish, and stop operations per session. Use one journal writer. Keep project/profile leases for cross-process ownership; defer concurrent operations within a session.
5. **One mandatory evidence mode:** record every strict MVP session. Reject `record_trace=false` for strict mode. Keep legacy behavior unchanged. This removes an unrecorded branch from every strict operation.

Keep named profiles, source-change detection, attestation, fail-stop ownership, native image output, and the real checkpoint/restart proof. These are required for the intended result, not optional platform work.

## Resolve the review findings

### Gate order: use synthetic code before either real-game launch

Gate A owns filesystem/process/display boundary proofs using synthetic payloads. Exercise writes to all designated bind destinations without depending on Ren'Py APIs. Use a minimal synthetic Ren'Py project for the display render probe. Do not execute MyPigPrincess's launcher or game scripts during Task 0.

Gate B owns actual Ren'Py save-location semantics, earliest-init persistent/MultiPersistent writes, bridge attestation, and the complete observe/action/checkpoint route. Add these tests incrementally during implementation; Gate B closes only when the whole fixture passes. Only then launch the real target.

Separate the fixture's intentional `extra_savedirs` escape into a negative launch case. The successful route must use valid save locations; an always-enabled escape would correctly prevent it from reaching ready.

### Evidence identity: make each observation an immutable capture

The simplest fix needs no additional public capture identifier:

- Mint a fresh opaque `observation_id` for every coherent observation returned to the caller, even if only pixels changed.
- Persist one immutable manifest at `observations/<observation_id>.json`, referencing its exact content-addressed `frame_id`.
- Store the semantic guard tuple (session, generations, signature, element table) with that observation. At action dispatch compare the saved tuple to the live tuple, not observation-token equality or pixel hashes.
- For `after_observation_id`, resolve the prior tuple and wait for a semantic/generation change. A fresh capture token by itself never satisfies `changed=true`.
- Frame-local element IDs resolve through that observation's element table. Reject unknown, inconsistent, foreign-session, or expired observations.

An animated background can produce distinct retained observations while leaving a previously observed control actionable. Two identical-looking successive interactions still invalidate the old guard through generation changes. Keep observed manifests immutable when adding journal metadata.

### Evidence durability: block input when required evidence cannot be retained

Before dispatch, durably retain the exact pre-action PNG and manifest and append/fsync `action.requested`. Check quotas and reserve room for bounded outcome/failure records before authorizing input. If any prerequisite write fails, return `evidence_unavailable` without dispatch. Optional polling captures may be omitted with explicit accounting; required action evidence may not be omitted.

After dispatch, append/fsync the outcome. A crash or storage failure can still occur between input and the outcome record: recover an unresolved request as `outcome_unknown`, never as success or automatic permission to retry. Stop further mutations and require a fresh observation after safe recovery. Do not claim exactly-once delivery across a process crash.

Consume an observation's action authorization on acceptance and block another action until the bridge has processed the pending input and a fresh observation is available. This prevents an immediate duplicate call from posting a second click before the interaction generation advances. Rejected calls do not dispatch input. Journal the chosen recovery policy in focused tests.

Allow `complete` only when all dispatched actions have required evidence and no unresolved outcomes remain. If the disk cannot record a failure marker, report the error and leave the trace unfinished; never fabricate durable completion. After finish, refuse further mutations except stop, which updates session lifecycle metadata without reopening the closed trace.

### Engine and CI: test the version used for the acceptance claim

Keep existing 8.5.3 tests. Add a separate Linux strict-play job targeting the full version `8.2.0.24012702` named by the real target. Provision an official runtime, verify its full version and artifact checksum, and package only the repository-owned synthetic fixture as a disposable development bundle with a launcher, `renpy/`, and platform libraries. Bootstrap may use an SDK to assemble that fixture; strict launch must still use the resulting bundle, with no managed-SDK runtime fallback.

First prove that a distributable matching runtime can be obtained and the selected isolation backend works on the runner. If either prerequisite fails, report the gate as blocked and resolve provisioning or runner choice; do not silently switch engine versions. No proprietary target files enter CI.

Install Bubblewrap, fuse-overlayfs/fusermount, Xvfb, and required runtime libraries explicitly. Run the capability probe before the suite. Strict sessions must create and own their display process; they must not borrow the job-wide `DISPLAY=:99` used by legacy tests. Required-gate mode fails on missing dependencies, capability denial, or skipped acceptance tests. Ordinary local tests can retain their opt-in behavior.

Retain only allowlisted synthetic logs, frames, and sanitized manifests as CI artifacts. Do not upload publication directories, bridge tokens, arbitrary profile trees, or trusted control metadata. Test MCP text/image delivery through the actual server/client transport, not only direct Python handler calls.

### Strict-session access: enforce rejection at the shared dispatch boundary

Add a strict-session capability check in shared client/session resolution and enforce the allowed command set in the strict bridge. Public legacy mutation tools, including advance, control, input, variable writes, eval, choice selection, editor writes, and Autopilot, must not control a strict session. An Autopilot attempt must be refused before its existing stop/relaunch behavior can run.

Route strict save/load and stop through explicitly session-bound paths. All other strict mutations go through guarded `renforge_act`. Test each public mutation surface against a strict session, plus stale session A calls after session B starts. Leave read-only legacy inspection access an explicit allowlist decision, not an accidental discovery fallback.

## Delivery sequence and stopping points

| Stage | Existing tasks | Reviewable result |
| --- | --- | --- |
| Prove the environment | 0 + fixture/bootstrap portion of 11 | Reproducible Gate A, one backend/display, exact target runtime available in CI; no real game launched. |
| Launch and stop safely | 1–6, minimal journal foundation from 10 | Private profile, strict bundled launch, attested ready state, complete teardown and owner-death proof. |
| Complete the synthetic loop | 7–10 + remainder of 11 | Native observe → guarded act → durable evidence → checkpoint → stop/relaunch/load → finish through MCP; Gate B passes. |
| Prove the real route | 12 | One local route, unchanged source/save sentinels, complete decision evidence and honest limitations. |

Define contracts immediately before the stage that consumes them; avoid publishing stub tools for features that cannot yet run. Preserve the original task numbers as a checklist, but split work by these testable results rather than parallel file ownership alone.

## Resume from actual repository state

- Spike commit `35ce6fe` remains on `play/task0-spike`; it is not part of the main merge. Review its diff and integrate it separately before claiming its tests are available on this branch.
- The recorded `/tmp/renforge-play-wt/` worktrees are missing and Git lists them as prunable. Recreate needed worktrees from the updated plan branch; do not assume the old workspaces exist.
- The August 31 9/9 spike pass and historical baseline failures are historical evidence, not current results. Rerun focused tests on the selected host/runner. Record exact failures rather than blanket-excluding suites based on the old note.
- No specific agent model or parallel orchestration is a prerequisite for execution. Keep edits to shared bridge/registration files serialized regardless of worker count.

This proposal changes planning only. Gates A/B remain open, and no strict-play production implementation or real-game acceptance run is implied.
