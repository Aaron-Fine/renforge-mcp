"""Exact-engine rendering and owned-display environment checks.

Required CI sets RENFORGE_STRICT_PLAY_TESTS=1 and points at the checksum-verified
bundle assembled by scripts/bootstrap_strict_play_runtime.py.
"""
from __future__ import annotations

import os
import signal
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image


pytestmark = pytest.mark.skipif(
    os.environ.get("RENFORGE_STRICT_PLAY_TESTS") != "1",
    reason="set RENFORGE_STRICT_PLAY_TESTS=1 for the required environment gate",
)


def _stop_group(process: subprocess.Popen, timeout: float = 5.0) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=timeout)


def _start_xvfb(tmp_path: Path) -> tuple[subprocess.Popen, str]:
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "960x540x24", "-nolisten", "tcp"],
        stdout=(tmp_path / "xvfb.stdout").open("wb"),
        stderr=(tmp_path / "xvfb.stderr").open("wb"),
        pass_fds=(write_fd,),
        start_new_session=True,
    )
    os.close(write_fd)
    try:
        with os.fdopen(read_fd, "rb", closefd=True) as stream:
            number = stream.readline(32).strip().decode("ascii")
    except BaseException:
        _stop_group(process)
        raise
    if not number:
        _stop_group(process)
        raise AssertionError("Xvfb did not publish a display number")
    return process, f":{number}"


def test_exact_engine_renders_native_frame_and_owned_groups_stop(tmp_path: Path) -> None:
    for executable in ("Xvfb", "xdpyinfo", "import"):
        assert shutil.which(executable), f"required executable missing: {executable}"
    bundle = Path(os.environ["RENFORGE_STRICT_PLAY_BUNDLE"]).resolve(strict=True)
    version = (bundle / "renpy" / "vc_version.py").read_text(encoding="utf-8")
    assert "version = '8.2.0.24012702'" in version

    xvfb, display = _start_xvfb(tmp_path)
    game: subprocess.Popen | None = None
    try:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DISPLAY": display,
            "SDL_AUDIODRIVER": "dummy",
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            "TMPDIR": str(tmp_path / "tmp"),
        }
        for path in ("home", "xdg-config", "xdg-cache", "xdg-data", "tmp", "saves"):
            (tmp_path / path).mkdir()
        game = subprocess.Popen(
            [str(bundle / "StrictPlay.sh"), "--savedir", str(tmp_path / "saves")],
            cwd=bundle,
            env=env,
            stdout=(tmp_path / "game.stdout").open("wb"),
            stderr=(tmp_path / "game.stderr").open("wb"),
            start_new_session=True,
        )
        deadline = time.monotonic() + 20
        frame = tmp_path / "frame.png"
        while time.monotonic() < deadline:
            assert game.poll() is None, (tmp_path / "game.stderr").read_text(errors="replace")
            result = subprocess.run(
                ["import", "-display", display, "-window", "root", str(frame)],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0 and frame.exists():
                image = Image.open(frame).convert("RGB")
                if image.size == (960, 540) and image.getbbox() is not None:
                    colors = image.getcolors(maxcolors=1_000_000)
                    if colors is not None and len(colors) > 2:
                        break
            time.sleep(0.2)
        else:
            raise AssertionError("exact-engine fixture did not render a coherent native frame")
    finally:
        if game is not None:
            _stop_group(game)
        _stop_group(xvfb)
    assert game is not None and game.poll() is not None
    assert xvfb.poll() is not None
