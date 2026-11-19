"""Follow-up fixes in bridge.rpy, run against the fake runtime of test_bridge_runtime.

These pin the bridge halves of fixes whose Python halves landed elsewhere: the
stuck-pause dismiss, the shared custom-action rule, one scene inventory per
semantic click, and engine widget subclasses in the kinetic classifier.
"""

from __future__ import annotations

import sys
import types

import pytest
from test_bridge_runtime import running_bridge  # noqa: F401 - shared bridge fixture


def _bridge():
    return sys.modules["_renforge_runtime"].bridge


# --- dismiss_if: the stuck-pause opt-in ---------------------------------------


def _pausing_line(running_bridge, monkeypatch, *, reason="pause", say_dismiss="dismiss"):
    """Serve a say line whose frame is unstable only because ``reason`` is pending."""
    bridge = _bridge()
    raw = {"ok": True, "interaction": bridge.observe_generation, "stable": False,
           "unstable_reason": reason, "session_id": bridge.session_id, "observation_id": "frame",
           "screens": [], "elements": [], "say_dismiss": say_dismiss,
           "dialogue": {"who": None, "what": "Wait for it."}}
    monkeypatch.setitem(running_bridge.globs, "_renforge_observe_raw", lambda *a, **k: dict(raw))
    running_bridge.renpy._queued_events[:] = []
    return raw


def test_dismiss_if_clicks_a_pending_pause_only_when_asked(running_bridge, monkeypatch):
    """A `{w=1.0}{nw}` line can stay a pause; the nudge's opt-in clicks it once."""
    raw = _pausing_line(running_bridge, monkeypatch)
    client = running_bridge.client

    refused = client.dismiss_if(interaction=raw["interaction"])
    assert refused == {"ok": False, "error": "unstable", "interaction": raw["interaction"]}
    assert running_bridge.renpy._queued_events == []

    posted = client.dismiss_if(interaction=raw["interaction"], allow_pause=True)
    assert posted.get("ok") is True, posted
    assert running_bridge.renpy._queued_events == ["dismiss"]


def test_dismiss_if_pause_opt_in_must_be_true_itself(running_bridge, monkeypatch):
    """Only a literal ``true`` opts in, as with ``past_optional_controls``."""
    raw = _pausing_line(running_bridge, monkeypatch)
    reply = running_bridge.client.request("dismiss_if", {"interaction": raw["interaction"], "allow_pause": "yes"})
    assert reply.get("ok") is False and reply.get("error") == "unstable", reply
    assert running_bridge.renpy._queued_events == []


@pytest.mark.parametrize(("reason", "say_dismiss", "error"), [
    ("transition", "dismiss", "unstable"),  # only a pause is let through
    ("pause", None, "not_dismiss"),  # a hard pause takes no click
])
def test_dismiss_if_pause_opt_in_keeps_every_other_check(running_bridge, monkeypatch, reason, say_dismiss, error):
    raw = _pausing_line(running_bridge, monkeypatch, reason=reason, say_dismiss=say_dismiss)
    reply = running_bridge.client.dismiss_if(interaction=raw["interaction"], allow_pause=True)
    assert reply.get("ok") is False and reply.get("error") == error, reply
    assert running_bridge.renpy._queued_events == []
