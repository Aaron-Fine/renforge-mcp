"""Play and progression in tools.live: time budgets and observation failures."""

from __future__ import annotations

from renforge.tools import live


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _continue_from_a_line(monkeypatch, *, timeout: float, observe_cost: float) -> tuple[dict, list[float]]:
    """Continue past a line with optional buttons; each observe takes *observe_cost* seconds."""
    clock = _Clock()
    monkeypatch.setattr(live.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(live.time, "sleep", lambda seconds: None)
    line = {"observation_id": "o1", "interaction": 3, "forward": "choose", "say_dismiss": "dismiss"}

    def fake_observe(*_args, **_kwargs):
        clock.now += observe_cost
        return dict(line)

    budgets: list[float] = []

    def fake_advance_until(*_args, timeout, **_kwargs):
        budgets.append(timeout)
        return {"ok": True, "stop": "choose", "steps": 1, "observation": dict(line), "observation_status": "ready"}

    monkeypatch.setattr(live, "observe", fake_observe)
    monkeypatch.setattr(live, "_with_client", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(live, "advance_until", fake_advance_until)
    return live.play("/tmp/renforge-play-budget", "o1", timeout=timeout), budgets


def test_continue_gives_progression_only_what_is_left_of_the_budget(monkeypatch) -> None:
    result, budgets = _continue_from_a_line(monkeypatch, timeout=4.0, observe_cost=0.5)

    assert result["clicked_past_line"] is True
    # The first observe and the wait for the line to end took 2.5 s of the 4.
    assert len(budgets) == 1 and abs(budgets[0] - 1.5) < 1e-6


def test_continue_reports_a_timeout_when_no_budget_is_left(monkeypatch) -> None:
    result, budgets = _continue_from_a_line(monkeypatch, timeout=0.4, observe_cost=0.3)

    assert budgets == []
    assert result["observation_status"] == "timeout"
    assert result["observation"] is None
    assert result["progress"]["stop"] == "timeout"



def test_continue_holds_observation_and_dismissal_to_its_deadline(monkeypatch) -> None:
    """With a short timeout the first observe, the click past the line, and the wait after it share one deadline."""
    clock = _Clock()
    monkeypatch.setattr(live.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(live.time, "sleep", lambda seconds: None)
    line = {"observation_id": "o1", "interaction": 3, "forward": "choose", "say_dismiss": "dismiss"}
    observed: list[float | None] = []
    dismissed: list[float | None] = []

    def fake_observe(_path, *, screenshot=True, deadline=None):
        observed.append(deadline)
        clock.now += 0.3
        return dict(line)

    class _Client:
        def dismiss_if(self, *, deadline=None, **_kwargs):
            dismissed.append(deadline)
            clock.now += 0.1
            return {"ok": True}

    budgets: list[float] = []

    def fake_advance_until(*_args, timeout, **_kwargs):
        budgets.append(timeout)
        return {"ok": True, "stop": "choose", "steps": 1, "observation": dict(line), "observation_status": "ready"}

    monkeypatch.setattr(live, "observe", fake_observe)
    monkeypatch.setattr(live, "_with_client", lambda _path, fn: fn(_Client()))
    monkeypatch.setattr(live, "advance_until", fake_advance_until)

    result = live.play("/tmp/renforge-play-deadline", "o1", timeout=1.0)

    end = 1000.0 + 1.0
    assert dismissed == [end]
    assert observed and all(deadline == end for deadline in observed), observed
    # The wait after the click stops at the deadline, not timeout/2 after the click.
    assert clock.now <= end + 0.3 + 1e-9
    # The accepted click is still reported when nothing is left for progression.
    assert result["clicked_past_line"] is True
    assert budgets == [] and result["progress"]["stop"] == "timeout"


def test_observe_passes_its_deadline_to_the_bridge(monkeypatch) -> None:
    seen = []

    class _Client:
        def observe(self, *, screenshot=False, deadline=None):
            seen.append(deadline)
            return {"ok": True, "interaction": 1, "stable": True}

    monkeypatch.setattr(live, "_client", lambda _path: _Client())
    monkeypatch.setattr(live, "_mark_crossed", lambda *_a, **_k: None)
    live.observe("/tmp/renforge-observe-deadline", screenshot=False, deadline=1234.5)
    live.observe("/tmp/renforge-observe-deadline", screenshot=False)
    assert seen == [1234.5, None]


def test_progression_reports_a_screenshot_that_does_not_decode(monkeypatch) -> None:
    """PIL raises OSError for a corrupt image; the run's transcript must still come back."""
    frame = {
        "interaction": 4, "stable": True, "unstable_reason": None,
        "statement": {"file": "game/script.rpy", "line": 22}, "label": "start",
        "dialogue": {"who": None, "what": "Hello"},
        "screens": [{"name": "say", "layer": "screens", "modal": False}],
        "say_dismiss": "dismiss", "overlay_screens": [], "elements": [],
        "omitted": {"focus_truncated": False, "unclassified": False}, "frame_hash": None,
    }

    class _Client:
        config = type("Cfg", (), {"host": "127.0.0.1", "port": 9, "token": "tok"})()

        def observe(self, screenshot=False, deadline=None):
            return {**frame, **({"screenshot": "corrupt"} if screenshot else {})}

        def progression(self, action, operation_id, **_kwargs):
            return {"ok": True, "progression": {"id": operation_id, "active": False, "stop": "none", "steps": 1}}

        def poll_events(self, since=0, deadline=None):
            return {"events": [{"seq": 1, "type": "label", "label": "start"}], "cursor": 1}

    def fake_apply_screenshot(reply):
        if "screenshot" in reply:
            raise OSError("image file is truncated")
        return dict(reply), None

    monkeypatch.setattr(live, "_client", lambda _path: _Client())
    monkeypatch.setattr(live, "apply_screenshot", fake_apply_screenshot)
    monkeypatch.setattr(live, "_owned_session", lambda _path: None)
    monkeypatch.setitem(live._crossed_after, live._key("/tmp/renforge-corrupt-shot"), (("x", 0, "y"), 0))

    result = live.advance_until("/tmp/renforge-corrupt-shot", max_steps=5, timeout=2, screenshot=True)

    assert result["observation_status"] == "transport_error"
    assert "invalid screenshot" in result["observation_error"]
    assert {"type": "label", "label": "start"} in result["crossed"]


def test_the_stuck_pause_nudge_asks_the_bridge_to_accept_a_pending_pause(monkeypatch) -> None:
    """A {w=1.0}{nw} line that stays a pause is clicked once; the plain guard refuses every pause."""
    from renforge import progression

    stuck = {
        "interaction": 7, "stable": False, "unstable_reason": "pause",
        "statement": {"file": "game/script.rpy", "line": 22}, "label": "start",
        "dialogue": {"who": None, "what": "Wait for it"},
        "screens": [{"name": "say", "layer": "screens", "modal": False}],
        "say_dismiss": "dismiss", "overlay_screens": [], "elements": [],
        "omitted": {"focus_truncated": False, "unclassified": False},
        "session_id": "s1", "observation_id": "o1",
    }
    dismisses: list[dict] = []

    class _Client:
        config = type("Cfg", (), {"host": "127.0.0.1", "port": 9, "token": "tok"})()

        def observe(self, screenshot=False, deadline=None):
            return dict(stuck)

        def dismiss_if(self, **kwargs):
            dismisses.append(kwargs)
            return {"ok": True}

        def progression(self, *_args, **_kwargs):
            raise AssertionError("progression must not start from an unstable frame")

        def poll_events(self, since=0, deadline=None):
            return {"events": [], "cursor": since}

    monkeypatch.setattr(progression, "STUCK_PAUSE_SECONDS", 0.05)
    monkeypatch.setattr(live, "_client", lambda _path: _Client())
    monkeypatch.setattr(live, "_owned_session", lambda _path: None)

    live.advance_until("/tmp/renforge-stuck-pause", max_steps=5, timeout=0.5, screenshot=False)

    assert len(dismisses) == 1
    assert dismisses[0]["interaction"] == 7
    assert dismisses[0]["allow_pause"] is True


def test_waiting_for_a_stable_observation(monkeypatch) -> None:
    """Shared by a session load and a mod reload: poll observe until the screen is stable."""
    replies: list[dict] = []
    monkeypatch.setattr(live, "observe", lambda *_a, **_k: replies.pop(0))
    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)

    replies[:] = [{"stable": False}, {"stable": True}, {"stable": True, "observation_id": "o2"}]
    assert live.wait_for_stable_observation("/tmp/renforge-wait") == {"stable": True}
    replies[:] = [{"stable": True}, {"stable": True, "observation_id": "o2"}]
    assert live.wait_for_stable_observation("/tmp/renforge-wait", require_id=True) == {
        "stable": True, "observation_id": "o2"}
    # Out of time, the last observation comes back after at least one look.
    replies[:] = [{"stable": False}, {"stable": True}]
    assert live.wait_for_stable_observation("/tmp/renforge-wait", timeout=0.0) == {"stable": False}
    assert replies == [{"stable": True}]
