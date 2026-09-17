# Linux sandbox contract test infrastructure

## Scope

This change adds test infrastructure for Linux filesystem and process isolation.
It does not add isolation to RenForge's application launcher, register strict-play
MCP tools, or establish that untrusted projects are safe to run. The sandbox
helper is a test-only prototype, not a production security boundary, and it is
not a launch prerequisite. MCP save/preference/HOME isolation is implemented
separately in `src/renforge/save_isolation.py`.

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

Keep the canaries, write-routing assertions, and descendant checks as
regression contracts for this **test-only** Linux helper. Do not wire
Bubblewrap, FUSE, or `scripts/sandbox_test_support.py` into MCP or dashboard
launch. Production `renforge_launch` isolation is application-layer
(`src/renforge/save_isolation.py`): a disposable session root with native
`--savedir`, a private HOME/XDG/temp tree, and empty persistent/preferences.
That path must stay independent of this sandbox prototype.

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

MCP/dashboard `renforge_launch` defaults to application-layer isolation
(temporary savedir, private HOME, empty persistent/preferences). That keeps
ordinary engine save, preference, and `~/.renpy` writes out of the user's
files, but it is **not** the Bubblewrap/FUSE sandbox and must not be described
as equivalent. Missing bwrap/FUSE must never fail an MCP launch.

## Future application work (not on the MCP launch path)

A Linux namespace/FUSE supervisor remains an optional research direction for
hostile-project execution. It is **not** a prerequisite for MCP save or
preference isolation, and it is not scheduled as production launcher work.
If that experiment resumes, keep it behind the existing `sandbox-contract` CI
job and `RENFORGE_SANDBOX_TESTS=1`; do not make it a launch dependency.

The current product goal is already served by application-layer isolation:
launch a supported project without touching the user's Ren'Py saves, HOME, or
preferences, observe coherent image and semantic state, and stop cleanly.

The later sandbox experiment, if pursued, would still need a fresh display,
profile lease, session, and trace; path validation without following
symlinks; restricted environment and network access; and treating all guest
output as untrusted. That work is out of scope for MCP launches.

## Stage 1: strict launch and stop (deferred)

This stage described a production Bubblewrap/FUSE vertical slice. It is
deferred. Do not implement a reusable sandbox builder on the launcher, do not
require a FUSE backend probe at preflight, and do not reject ordinary MCP
launch when namespace tools are absent.

The `sandbox-contract` CI job may keep exercising the test helper so the
prototype does not rot. Application-layer isolation tests live in
`tests/test_save_isolation.py` and the launch-path unit tests.

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
