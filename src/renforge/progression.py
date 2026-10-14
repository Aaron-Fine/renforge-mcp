"""Host progression, settling, and failure handling for both gameplay modes."""
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


def settle(fetch, deadline, *, screenshot=True):
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
            last = framed
            previous = None
        else:
            previous = identity if last["stable"] else None
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    return None, "timeout", "final observation did not settle within the budget"


def run_native_skip(client, fetch, *, max_steps, deadline, total_deadline, between=None):
    """Start one bridge-owned skip operation and wait for it to stop.

    Once started, poll only the operation's status (and ``between``, which
    drains events): a full observation per poll would compete with skipping
    for the game's main thread. The caller settles the final observation.
    """
    operation_id = uuid.uuid4().hex
    attempted = False
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
                status = reply.get("progression", {})
                result["steps"] = status.get("steps", 0)
                if status.get("active") is False:
                    result["stop"] = status.get("stop") or "none"
                    # A custom hub is a normal place to stop and hand control back.
                    result["ok"] = result["stop"] != "crash"
                    break
                time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
                continue
            raw = fetch(False)
            if raw.get("ok") is False:
                result.update(ok=False, stop=raw.get("status", "transport_error"), error=raw.get("error"))
                break
            snapshot = classify(raw)
            result["observation"] = snapshot
            if not attempted:
                if not snapshot["stable"]:
                    time.sleep(POLL_SECONDS)
                    continue
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
            if status.get("id") == operation_id:
                result["steps"] = status.get("steps", 0)
                if status.get("active") is False:
                    result["stop"] = status.get("stop") or "none"
                    # A custom hub is a normal place to stop and hand control back.
                    result["ok"] = result["stop"] != "crash"
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


def _finished(stop: str, steps: int, observation: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "ok": True,
        "stop": stop,
        "steps": steps,
        "observation": observation,
    }


def run_advance_until(
    fetch: Callable[[bool], Mapping[str, Any]],
    dismiss: Callable[[int], Mapping[str, Any]],
    *,
    max_steps: int,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Post ``dismiss`` until the snapshot says the player must choose.

    ``fetch(screenshot)`` returns raw bridge records. ``dismiss(interaction)``
    re-checks on the main thread and posts the event only when it still listens
    for ``dismiss``. This loop never clicks and never enables skip.
    """
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 200:
        return {
            "ok": False,
            "stop": "invalid_arguments",
            "steps": 0,
            "observation": None,
            "error": "max_steps must be an integer from 1 to 200",
        }
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        timeout_ok = False
    else:
        timeout_ok = timeout >= 0.1 and timeout <= 120
    if not timeout_ok:
        return {
            "ok": False,
            "stop": "invalid_arguments",
            "steps": 0,
            "observation": None,
            "error": "timeout must be a number from 0.1 to 120 seconds",
        }

    deadline = now() + float(timeout)
    steps = 0
    last: dict[str, Any] | None = None

    def look(screenshot: bool) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        nonlocal last
        raw = fetch(screenshot)
        if not isinstance(raw, Mapping) or raw.get("ok") is False:
            error = raw.get("error") if isinstance(raw, Mapping) else "observe failed"
            return None, {
                "ok": False,
                "stop": raw.get("status", "transport_error") if isinstance(raw, Mapping) else "transport_error",
                "steps": steps,
                "observation": last,
                "error": error if isinstance(error, str) else "observe failed",
            }
        snapshot = classify(raw)
        last = snapshot
        return snapshot, None

    def shot(fallback: dict[str, Any] | None) -> dict[str, Any] | None:
        snapshot, failure = look(True)
        if failure is not None or snapshot is None or not snapshot["stable"]:
            return fallback
        return snapshot

    while True:
        if now() >= deadline:
            return _finished("timeout", steps, shot(last))
        snapshot, failure = look(False)
        if failure is not None:
            return failure
        assert snapshot is not None
        if not snapshot["stable"]:
            sleep(POLL_SECONDS)
            continue
        forward = snapshot["forward"]
        boundary = progression_boundary(snapshot)
        if boundary in ("end", "main_menu", "unsupported"):
            return _finished(boundary, steps, shot(snapshot))
        if forward == "wait":
            seen = snapshot["interaction"]
            changed = False
            while now() < deadline:
                sleep(POLL_SECONDS)
                nxt, failure = look(False)
                if failure is not None:
                    return failure
                assert nxt is not None
                if nxt["interaction"] != seen or (nxt["stable"] and nxt["forward"] != "wait"):
                    changed = True
                    break
            if not changed:
                return _finished("wait", steps, shot(snapshot))
            continue
        if forward != "dismiss":
            framed, failure = look(True)
            if failure is not None:
                return failure
            assert framed is not None
            if not framed["stable"]:
                sleep(POLL_SECONDS)
                continue
            if framed["forward"] == "dismiss":
                snapshot = framed
            else:
                stop = framed["forward"]
                if stop not in {"choose", "wait", "none"}:
                    stop = "none"
                return _finished(stop, steps, framed)
        if snapshot["forward"] != "dismiss" or not snapshot["stable"]:
            continue
        if steps >= max_steps:
            return _finished("max_steps", steps, shot(snapshot))
        interaction = snapshot["interaction"]
        if not isinstance(interaction, int):
            return _finished("none", steps, shot(snapshot))
        reply = dismiss(interaction)
        if not isinstance(reply, Mapping) or reply.get("ok") is False:
            error = reply.get("error") if isinstance(reply, Mapping) else None
            if error in {"unstable", "stale", "not_dismiss"}:
                sleep(POLL_SECONDS)
                continue
            return {
                "ok": False,
                "stop": reply.get("status", "transport_error") if isinstance(reply, Mapping) else "transport_error",
                "steps": steps,
                "observation": snapshot,
                "error": error if isinstance(error, str) else "dismiss failed",
            }
        steps += 1
        advanced = False
        while now() < deadline:
            sleep(POLL_SECONDS)
            nxt, failure = look(False)
            if failure is not None:
                return failure
            assert nxt is not None
            if nxt["interaction"] != interaction:
                advanced = True
                break
            if snapshot.get("text_revealing") is True and nxt.get("text_revealing") is False and nxt["stable"]:
                # The first dismiss reveals slow text in the same interaction.
                # Re-check the inventory before posting the next guarded dismiss.
                advanced = True
                break
        if not advanced:
            return _finished("stalled", steps, shot(snapshot))
