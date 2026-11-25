"""Host progression (bounded native skip), settling, and failure handling."""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .bridge.client import BridgeError, BridgeProtocolError
from .interaction import classify, progression_boundary
from .util.frames import FRAME_TIMEOUT

POLL_SECONDS = 0.05


def process_exit(process) -> dict | None:
    """Only a retained owned process can establish termination evidence."""
    if process is None:
        return None
    try:
        code = process.poll()
    except Exception:
        return None
    if isinstance(code, int) and not isinstance(code, bool):
        return {"phase": "exited", "exit_code": code, "reason": "clean_exit" if code == 0 else "nonzero_exit"}
    return None


def failure_status(error: Exception, process=None) -> str:
    """Name a bridge failure: ``exited`` or ``crash`` when the owned process
    ended, ``timeout`` for a timed-out request, else ``transport_error``."""
    exited = process_exit(process)
    if exited is not None:
        return "exited" if exited["exit_code"] == 0 else "crash"
    if (isinstance(error, BridgeProtocolError) and error.code == FRAME_TIMEOUT) or any(word in str(error).lower() for word in ("timed out", "timeout")):
        return "timeout"
    return "transport_error"


# Stable observations of one interaction that still differ (a screen whose
# timer rebuilds its buttons, such as a music room's playback bar) settle
# after this many in a row.
SETTLE_SAME_INTERACTION = 4


def _busy(raw: Mapping[str, Any], deadline: float) -> bool:
    """A reply that timed out while budget remains: the game's main thread was
    busy (decoding a new scene's images, say), so asking again is safe."""
    return raw.get("status") == "timeout" and deadline - time.monotonic() > POLL_SECONDS


Fetch = Callable[[bool], Mapping[str, Any]]


def settle(fetch: Fetch, deadline: float, *,
           screenshot: bool = True) -> tuple[dict[str, Any] | None, str, str | None]:
    """Require two matching stable observations after progression is disabled.

    A screen that keeps changing within one stable interaction never matches
    itself, so a run of stable observations of that interaction is enough.
    """
    previous = None
    last = None
    streak = 0
    while time.monotonic() < deadline:
        raw = fetch(False)
        if raw.get("ok") is False:
            if _busy(raw, deadline):
                time.sleep(POLL_SECONDS)
                continue
            return None, raw.get("status", "transport_error"), raw.get("error")
        before = last
        last = classify(raw)
        identity = (last["interaction"], last["observation_id"])
        if last["stable"] and before is not None and before["stable"] and before["interaction"] == last["interaction"]:
            streak += 1
        else:
            streak = 0
        if last["stable"] and (identity == previous or streak >= SETTLE_SAME_INTERACTION - 1):
            if not screenshot:
                return last, "ready", None
            shot = fetch(True)
            if shot.get("ok") is False:
                return last, shot.get("status", "transport_error"), shot.get("error")
            framed = classify(shot)
            if framed["stable"] and (framed["interaction"], framed["observation_id"]) == identity:
                return framed, "ready", None
            if framed["stable"] and framed["interaction"] == last["interaction"] and identity != previous:
                return framed, "ready", None
            # The screenshot may show a new interaction: its run starts here.
            last = framed
            previous = None
            streak = 0
        else:
            previous = identity if last["stable"] else None
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    if last is not None and not last["stable"] and last.get("unstable_reason") in ("pause", "transition"):
        # The budget ended inside a pause or transition: that is the current
        # state, and its unstable_reason says so. Continuing waits it out.
        return last, "ready", None
    return None, "timeout", "final observation did not settle within the budget"


def _finish_from_status(result: dict[str, Any], status: Mapping[str, Any]) -> bool:
    """Copy the operation's step count; True (with the stop recorded) once it ended."""
    result["steps"] = status.get("steps", 0)
    if status.get("active") is not False:
        return False
    result["stop"] = status.get("stop") or "none"
    # A custom hub is a normal place to stop and hand control back.
    result["ok"] = result["stop"] != "crash"
    return True


# Longer than any short pause (5 s) plus its grace: a "pause" still pending
# after this is stuck, and a line the player can click past is clicked once.
STUCK_PAUSE_SECONDS = 6.0


def run_native_skip(client: Any, fetch: Fetch, *, max_steps: int, deadline: float, total_deadline: float,
                    between: Callable[[], Any] | None = None,
                    nudge: Callable[[int | None], Any] | None = None) -> dict[str, Any]:
    """Start one bridge-owned skip operation and wait for it to stop.

    Once started, poll only the operation's status (and ``between``, which
    drains events): a full observation per poll would compete with skipping
    for the game's main thread. The caller settles the final observation.

    ``nudge(interaction)`` is called once when one dismissible pause has
    lasted ``STUCK_PAUSE_SECONDS``, timed from its first frame. It must post a guarded dismiss that accepts a
    pending pause (``dismiss_if(..., allow_pause=True)``); the plain guard
    refuses every unstable frame.
    """
    operation_id = uuid.uuid4().hex
    attempted = False
    # The dismissible pause being timed, and when it first appeared. Any
    # other frame (a transition, a stable one, another interaction's pause)
    # ends it, so the time before a pause never counts toward it.
    pause_interaction: object = None
    pause_since: float | None = None
    nudged = False
    result = {"ok": True, "stop": "timeout", "steps": 0, "observation": None, "mode": "skip"}
    try:
        while time.monotonic() < deadline:
            if attempted:
                # The overall deadline leaves room to read the last status;
                # running out of budget is a stop, not a transport failure.
                reply = client.progression("status", operation_id, deadline=total_deadline)
                if reply.get("ok") is False:
                    result.update(ok=False, stop="unsupported", error=reply.get("error"))
                    break
                if between is not None:
                    between()
                if _finish_from_status(result, reply.get("progression", {})):
                    break
                time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
                continue
            raw = fetch(False)
            if raw.get("ok") is False:
                if _busy(raw, deadline):
                    time.sleep(POLL_SECONDS)
                    continue
                result.update(ok=False, stop=raw.get("status", "transport_error"), error=raw.get("error"))
                break
            snapshot = classify(raw)
            result["observation"] = snapshot
            if not attempted:
                if not snapshot["stable"]:
                    now = time.monotonic()
                    if snapshot.get("unstable_reason") == "pause" and snapshot.get("say_dismiss") == "dismiss":
                        if pause_since is None or pause_interaction != snapshot["interaction"]:
                            pause_interaction, pause_since = snapshot["interaction"], now
                        # A {w=1.0}{nw} line stayed a "pause"
                        # for the whole budget, yet one click moved it on.
                        if nudge is not None and not nudged and now - pause_since > STUCK_PAUSE_SECONDS:
                            nudged = True
                            nudge(snapshot["interaction"])
                    else:
                        pause_interaction, pause_since = None, None
                    time.sleep(POLL_SECONDS)
                    continue
                pause_interaction, pause_since = None, None
                boundary = progression_boundary(snapshot)
                if boundary is not None:
                    result["stop"] = boundary
                    break
                if deadline - time.monotonic() < 0.05:
                    break
                attempted = True
                reply = client.progression("start", operation_id, max_steps=max_steps,
                    timeout=max(0.05, deadline - time.monotonic()),
                    expires_at=time.time() + max(0, deadline - time.monotonic()),
                    interaction=snapshot["interaction"], session_id=snapshot["session_id"],
                    observation_id=snapshot["observation_id"], deadline=deadline)
                if reply.get("ok") is False:
                    if reply.get("error") == "expired":
                        result["stop"] = "timeout"
                        break
                    if reply.get("error") in ("stale", "unstable"):
                        attempted = False
                        continue
                    result.update(ok=False, stop="unsupported", error=reply.get("error"))
                    break
                status = reply.get("progression", {})
            if status.get("id") == operation_id and _finish_from_status(result, status):
                break
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    except BridgeError as exc:
        result.update(ok=False, stop=failure_status(exc), error=str(exc))
    finally:
        if attempted:
            try:
                stopped = client.progression("stop", operation_id, reason=result["stop"], deadline=total_deadline)
                if stopped.get("ok"):
                    result["steps"] = stopped.get("progression", {}).get("steps", result["steps"])
                    result["cleanup"] = "complete"
                elif stopped.get("error") == "stale_operation":
                    result["cleanup"] = "not_owned"
                else:
                    result["cleanup"] = "lease_expiry_pending"
            except BridgeError:
                # The bridge enforces the same deadline even if the host loses
                # its connection. Never change another caller's operation.
                result["cleanup"] = "lease_expiry_pending"
    return result
