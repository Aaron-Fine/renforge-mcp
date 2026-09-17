from __future__ import annotations

from pathlib import Path

import pytest

from renforge.play.trace import TraceJournal


def test_journal_recovers_complete_prefix_and_continues(tmp_path: Path) -> None:
    journal = TraceJournal(tmp_path, "trace-a", {"project_id": "p"})
    journal.append("trace.started")
    journal.append("session.launching", {"session_id": "s"})
    with journal.events.open("ab") as stream:
        stream.write(b'{"sequence":3')
    reopened = TraceJournal(tmp_path, "trace-a", {"project_id": "p"})
    assert [event["event"] for event in reopened.read_events(reopened.events)] == [
        "trace.started", "session.launching"
    ]
    reopened.append("session.stopped")
    assert [event["sequence"] for event in reopened.read_events(reopened.events)] == [1, 2, 3]


def test_finish_is_immutable(tmp_path: Path) -> None:
    journal = TraceJournal(tmp_path, "trace-b", {})
    journal.finish("failed", "fixture")
    with pytest.raises(ValueError, match="already finished"):
        journal.finish("complete", "rewrite")
