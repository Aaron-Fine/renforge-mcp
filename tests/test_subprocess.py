from __future__ import annotations

import os
import sys
import time

import pytest

from renforge.util.subprocess import run_command


def test_run_command_caps_stdout_before_capture() -> None:
    result = run_command(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 200000)",
        ],
        timeout=30,
        max_output_bytes=1000,
    )

    assert result.returncode == 0
    assert result.stdout_truncated is True
    assert result.stderr_truncated is False
    assert len(result.stdout.encode("utf-8")) <= 1000
    assert result.stdout.startswith("xxx")


def test_run_command_caps_stderr_before_capture() -> None:
    result = run_command(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('e' * 200000)",
        ],
        timeout=30,
        max_output_bytes=512,
    )

    assert result.returncode == 0
    assert result.stderr_truncated is True
    assert result.stdout_truncated is False
    assert len(result.stderr.encode("utf-8")) <= 512


def test_run_command_bounded_keeps_small_output() -> None:
    result = run_command(
        [sys.executable, "-c", "print('ok')"],
        timeout=30,
        max_output_bytes=1024,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "ok"
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False


def test_run_command_bounded_still_reports_timeouts() -> None:
    result = run_command(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=0.2,
        max_output_bytes=1024,
    )

    assert result.timed_out is True
    assert result.returncode == -1
    assert "timed out" in result.stderr


@pytest.mark.skipif(os.name != "posix", reason="uses sh to leave a descendant holding the pipes")
def test_run_command_bounded_keeps_output_when_a_descendant_holds_the_pipes(
    monkeypatch,
) -> None:
    import renforge.util.subprocess as subprocess_module

    # raising=False so this test also runs against code without the constant.
    monkeypatch.setattr(subprocess_module, "_READER_JOIN_GRACE_SECONDS", 1.0, raising=False)
    started = time.monotonic()

    result = run_command(
        ["sh", "-c", "sleep 15 & echo hi; echo err >&2"],
        timeout=10,
        max_output_bytes=1000,
    )

    elapsed = time.monotonic() - started
    # The direct child's output arrives even though the pipes never reach EOF.
    assert result.stdout == "hi\n"
    assert result.stderr == "err\n"
    # The capture is still incomplete, so the verdict stays honest.
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    # Both readers share one grace period instead of waiting one after the other.
    assert elapsed < 1.8
