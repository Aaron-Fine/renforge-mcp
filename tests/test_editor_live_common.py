from __future__ import annotations

import pytest

import renforge.editor_live_common as live_common


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ({"x": 100, "y": 100, "width": 77, "height": 44}, (110, 142)),
        ({"x": 4, "y": 8, "width": 1, "height": 1}, (4, 8)),
        ({"x": 4, "y": 8, "width": 3, "height": 2}, (6, 8)),
    ],
)
def test_focusable_edge_point_stays_inside_bounds(bounds: dict, expected: tuple[int, int]) -> None:
    assert live_common.focusable_edge_point(bounds) == expected


def test_select_lock_can_target_focusable_edge(monkeypatch) -> None:
    calls = []

    class Client:
        def request(self, name: str, payload: dict) -> dict:
            calls.append((name, payload))
            return {"ok": False, "lock_reason": "CONTAINER_POSITION_UNSUPPORTED"}

    monkeypatch.setattr(
        live_common,
        "wait_bounds",
        lambda *_args, **_kwargs: {"x": 100, "y": 100, "width": 77, "height": 44},
    )

    result = live_common.select_lock(
        Client(),
        "container",
        "CONTAINER_POSITION_UNSUPPORTED",
        fixture_screen="fixture",
        prefer_focusable_edge=True,
    )

    assert result == "CONTAINER_POSITION_UNSUPPORTED"
    assert calls == [("editor_task0_select", {"x": 110, "y": 142})]


class _StatusClient:
    """Replies to editor_task0_status from a script; an exception entry is raised."""

    def __init__(self, replies):
        self.replies = list(replies)

    def request(self, name, payload):
        assert name == "editor_task0_status"
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_wait_for_transaction_forgets_a_bridge_failure_the_bridge_recovered_from(
    tmp_path, monkeypatch
) -> None:
    from renforge.bridge.client import BridgeProtocolError

    monkeypatch.setattr(live_common.time, "sleep", lambda _s: None)
    source = tmp_path / "gui.rpy"
    source.write_bytes(b"old")
    pending = {"status_code": "reload_pending", "save_in_progress": True}
    client = _StatusClient([BridgeProtocolError("reloading"), pending])

    status, bridge_failure = live_common.wait_for_transaction(client, source, b"new", timeout=0.05)

    assert status == pending
    assert bridge_failure is None


def test_wait_for_transaction_does_not_blame_the_bridge_for_an_unreadable_source(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(live_common.time, "sleep", lambda _s: None)
    missing = tmp_path / "gui.rpy"  # mid-replace: briefly absent
    committed = {"status_code": "reload_committed", "save_in_progress": False}

    status, bridge_failure = live_common.wait_for_transaction(_StatusClient([committed]), missing, b"new", timeout=0.05)

    assert status == committed
    assert bridge_failure is None


def test_wait_for_transaction_reports_a_bridge_that_stays_down(tmp_path, monkeypatch) -> None:
    from renforge.bridge.client import BridgeError

    monkeypatch.setattr(live_common.time, "sleep", lambda _s: None)
    source = tmp_path / "gui.rpy"
    source.write_bytes(b"old")

    status, bridge_failure = live_common.wait_for_transaction(_StatusClient([BridgeError("gone")]), source, b"new", timeout=0.05)

    assert status is None
    assert bridge_failure == "BridgeError: gone"
