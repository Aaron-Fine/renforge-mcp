from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_strict_play_runtime.py"
SPEC = importlib.util.spec_from_file_location("strict_runtime_bootstrap", SCRIPT)
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


def test_runtime_pin_is_exact_and_uses_official_https_source() -> None:
    assert bootstrap.RENPY_FULL_VERSION == "8.2.0.24012702"
    assert bootstrap.ARCHIVE_URL == "https://www.renpy.org/dl/8.2.0/renpy-8.2.0-sdk.tar.bz2"
    assert bootstrap.ARCHIVE_SHA256 == "e79ec1014bad6adba69336f90802e89ae7a70172c1bb9c15301267390e9d7b38"


def test_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / bootstrap.ARCHIVE_NAME
    archive.write_bytes(b"not the official archive")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        bootstrap.verify_archive(archive)


def test_fixture_has_environment_gate_marker() -> None:
    fixture = (
        SCRIPT.parents[1] / "examples" / "strict_play_environment_game" / "game"
    )
    script = (fixture / "script.rpy").read_text(encoding="utf-8")
    assert "Strict play environment gate." in script
