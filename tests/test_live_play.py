"""renforge_play's continue path keeps to the caller's time budget."""

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
