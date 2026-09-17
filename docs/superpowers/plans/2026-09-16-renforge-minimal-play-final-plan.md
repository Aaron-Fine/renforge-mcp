# Strict minimal-play implementation plan

## Goal

Let an MCP client launch a supported Ren'Py project in an isolated profile,
observe coherent image and semantic state, make guarded choices, resume from a
verified checkpoint, and retain an auditable trace. A real target project must
not run until the synthetic route passes.

## Supported configuration

- Linux host with Bubblewrap, `fuse-overlayfs`, `fusermount3`, and Xvfb.
- Bundled Ren'Py `8.2.0.24012702`, obtained from the official archive and
  verified with the committed SHA-256 checksum.
- Loose `.rpy` source and one contained bundled launcher.
- One fresh display, sandbox, profile lease, session, and trace per launch.

The guest receives a copy-on-write project view, designated profile save
directories, private home/XDG/temp directories, minimal devices and read-only
system paths. It never receives the host home, normal saves, other profiles,
or trusted RenForge state. Guest publication is isolated from trusted control
files and is always parsed as untrusted input.

## Current environment gate

This gate proves only that the selected runtime and isolation primitives work
on a supported host. It deliberately contains no public MCP API or production
session lifecycle.

- [x] Merge current `main` into the feature branch.
- [x] Pin and checksum the exact official Ren'Py runtime.
- [x] Assemble a repository-owned synthetic render fixture.
- [x] Prove host-side FUSE copy-on-write with an allowlisted Bubblewrap
  namespace.
- [x] Prove host saves and trusted state are absent from the guest.
- [x] Prove project writes cannot modify the lower project and persistent
  writes land only in designated directories.
- [x] Prove required system mounts are read-only.
- [x] Prove detached guest descendants die with the namespace leader.
- [x] Prove a fresh Xvfb renders a nonblank native `960x540` frame using the
  pinned engine.
- [x] Run the environment gate as required pull-request CI.

The fixture stays minimal until interaction work begins. The environment test
helper is test support, not the production launcher.

## Stage 1: strict launch and stop

Build one production vertical slice: preflight, create a profile, launch the
synthetic fixture, query status, and stop it.

- Add contracts only when their public operation is implemented.
- Implement one reusable sandbox builder from the environment-gate behavior.
- Run a disposable backend probe during preflight; executable presence alone
  is not sufficient.
- Validate paths without following symlinks, reject special files and escaping
  binds, and close unintended file descriptors.
- Give one supervisor explicit ownership of the game namespace, Xvfb, FUSE
  helper, and mount.
- Allocate durable project, profile, session, and trace identities before
  asynchronous launch.
- Attest the live engine version, project paths, save paths, home/XDG/temp, and
  publication path before reporting ready.
- On stop or startup failure, reap every owned process and verify unmount. If
  cleanup cannot be proved, quarantine the session and refuse profile reuse.
- Reject legacy launch or attachment paths for a strict session.

Stage 1 exits when the bundled synthetic game reaches attested ready and stops
without changing lower-project or normal-save canaries. Tests must kill the
actual supervisor and independently fail game, display, and FUSE startup.

## Stage 2: observe, act, and resume

Extend the same fixture only as each public operation is implemented.

- Capture semantic state and its native PNG at a proven render-coherent engine
  point; store immutable observations identified by opaque IDs.
- Require a current stable observation for input. Resolve element IDs through
  that observation and reject stale, consumed, foreign, or disabled targets.
- Prove engine-side input acknowledgment. A timeout is an unknown outcome and
  must never be retried automatically.
- Journal requests before dispatch and outcomes afterward with ordered,
  durable JSONL records. Keep frames content-addressed and bounded by quota.
- Save and load one named checkpoint with project, profile, runtime, source,
  observation, and trace identity. Verify restored state before accepting it.
- Support clean stop/relaunch/resume without reusing session IDs or action
  authorization.

Stage 2 exits when a real MCP client completes the deterministic synthetic
route, including negative, crash, stale-action, and checkpoint recovery cases,
using only public operations.

## Stage 3: real-project proof

Run one disposable copy of the real project only after Stages 1 and 2 pass.
Preflight must prove source completeness; compiled-only or ambiguous content is
refused. Record the exact project/runtime fingerprint, complete one bounded
route, stop cleanly, and verify that the source and normal-save canaries remain
unchanged.

## Rules that apply to every stage

- Missing isolation, runtime, ownership, evidence, or attestation prerequisites
  fail closed.
- Every live operation uses `session_id`; project paths do not select a running
  strict session.
- Source changes taint the session and block further mutation or successful
  completion.
- Stop remains available even after evidence, input, or source failures.
- Tests use only bounded waits and bounded process creation.
- CI skips are allowed for optional local runs; the required environment job
  fails when a prerequisite is missing.
