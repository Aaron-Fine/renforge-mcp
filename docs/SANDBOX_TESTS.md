# Linux sandbox contract test infrastructure

## Scope

This change adds test infrastructure for Linux filesystem and process isolation.
It does not add isolation to RenForge's application launcher, register strict-play
MCP tools, or establish that untrusted projects are safe to run. The sandbox
helper is a test-only prototype, not a production security boundary.

Engine compatibility reuses the Ren'Py **8.5.3** harness already on `main`.
There is no separate SDK downloader, runtime cache, bundled launcher, or render
fixture for this test infrastructure.

## Two existing test layers

| Layer | Implementation | What it establishes |
| --- | --- | --- |
| Linux sandbox contracts (new) | `tests/test_sandbox_contract.py`, using `scripts/sandbox_test_support.py` | Filesystem layout, copy-on-write routing, selected host-state canaries, and PID-namespace descendant cleanup with shell guests |
| Ren'Py engine integration (reused from main) | `tests/test_integration_sdk.py` and `tests/test_integration_sdk_live_demo_acceptance.py` | Real-engine startup, bridge behavior, screenshots, and demo/editor acceptance through the existing SDK harness |

The `sandbox-contract` CI job needs Bubblewrap, `fuse-overlayfs`, `fusermount3`,
`/dev/fuse`, and pytest. It does not install or launch Ren'Py or Xvfb.
`RENFORGE_SANDBOX_TESTS=1` makes missing prerequisites fail in this job.
The backend probe runs once within the tests; there is no duplicate CI probe.

The unchanged `engine-integration` CI job owns the Ren'Py 8.5.3 SDK cache,
Xvfb display, and engine-test artifacts. It uses `RENFORGE_SDK_TESTS=1`,
`RENFORGE_SDK_VERSION=8.5.3`, and the existing `renforge.sdk` provisioning code.

These layers are independent. A green result does **not** demonstrate that
Ren'Py runs inside the sandbox, that Ren'Py saves follow the shell-tested paths,
or that a production supervisor cleans up every owned resource.

## Running the tests

On a Linux host with the sandbox prerequisites:

```sh
RENFORGE_SANDBOX_TESTS=1 python -m pytest -q -rs tests/test_sandbox_contract.py
```

Without the opt-in, local tests skip when sandbox prerequisites are absent.
Kernel/security policy must also permit the namespace and FUSE operations;
installed executables alone do not establish that capability. CI configures
its disposable runner accordingly; this is not a host-hardening recipe.

To run the existing engine suite with a working display, use the same harness
and SDK version as the `engine-integration` job:

```sh
RENFORGE_SDK_TESTS=1 RENFORGE_SDK_VERSION=8.5.3 python -m pytest -q -rs \
  tests/test_integration_sdk.py \
  tests/test_integration_sdk_live_demo_acceptance.py
```

See `.github/workflows/ci.yml` for display setup, SDK caching, and diagnostics.

## Reuse and limits

Keep the canaries, write-routing assertions, and descendant checks as regression
contracts. When production isolation is implemented, evolve the command builder
into one shared implementation and point these tests at it. Do not leave a
second test-only sandbox passing while the real launcher follows another path.

Current evidence is deliberately narrow:

- Project writes use a FUSE copy-on-write view; selected save/profile writes go
  to designated directories rather than the lower project.
- Selected normal-save and trusted-state canary paths are absent from the guest.
- System bind arguments are read-only and a write under `/usr` is rejected.
  The latter alone cannot distinguish a read-only mount from ordinary permission
  denial; a controlled writable canary remains useful future strengthening.
- Detached descendants are checked after the Bubblewrap process is killed.
  This does not test death of a production supervisor, Xvfb, or the FUSE helper.
- The helper inherits environment variables and shares the host network; it is
  not ready for hostile project execution. Path validation, bounded failure
  cleanup, resource ownership, and broader isolation tests remain future work.

## Future application work (not implemented by this PR)

The eventual goal is an MCP client that launches a supported project in an
isolated profile, observes coherent image and semantic state, makes guarded
choices, resumes verified checkpoints, and retains an auditable trace.
Use the shared Ren'Py 8.5.3 harness and extend the repository demo with focused
synthetic cases as needed, rather than introducing another SDK bootstrap.
A real target project must not run until the synthetic route passes.

The intended application boundary gives each launch a fresh display, sandbox,
profile lease, session, and trace. It must validate loose source and launcher
paths, restrict environment and network access, separate guest publication from
trusted control files, and treat all guest output as untrusted.

## Stage 1: strict launch and stop

Build one production vertical slice: preflight, create a profile, launch the
synthetic fixture, query status, and stop it.

- Add contracts only when their public operation is implemented.
- Implement one reusable sandbox builder from the sandbox contract behavior.
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

Stage 1 exits when the synthetic game using the shared Ren'Py SDK harness reaches attested ready and stops
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
- CI skips are allowed for optional local runs; the required sandbox contract job
  fails when a prerequisite is missing.
