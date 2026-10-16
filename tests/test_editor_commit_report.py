"""The live commit summary stays short enough to survive the Actions log."""

from renforge.editor_live_common import summarize_commit_report


def test_summarize_commit_report_keeps_the_handshake_fields() -> None:
    text = summarize_commit_report(
        {
            "error": "commit timeout",
            "verdict": "fail",
            "bridge_failure": "BridgeError: [Errno 111] Connection refused",
            "last_status": {
                "status_code": "commit_queued",
                "save_in_progress": True,
                "save_error": None,
                "status_text": "Commit queued",
                "capabilities": {"move": True},
            },
            "save_click": {"ok": True, "element": {"bounds": [0, 0, 1, 1]}},
        }
    )

    assert "error='commit timeout'" in text
    assert "verdict='fail'" in text
    assert "last_status_code='commit_queued'" in text
    assert "save_in_progress=True" in text
    assert "status_text='Commit queued'" in text
    assert "bridge_failure='BridgeError: [Errno 111] Connection refused'" in text
    assert "save_click_ok=True" in text
    assert "capabilities" not in text
    assert "bounds" not in text
