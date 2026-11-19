"""Shared test setup."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_renforge_state(tmp_path_factory, monkeypatch):
    # RenForge keeps per-project state outside the game; never use the real one.
    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path_factory.mktemp("renforge-state")))


@pytest.fixture(autouse=True)
def _all_tools_listed(monkeypatch):
    # A developer's RENFORGE_TOOLSETS must not change what the tests see.
    monkeypatch.delenv("RENFORGE_TOOLSETS", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    # RENFORGE_ISOLATION changes launch defaults server-wide. A developer who
    # exports it must not change what the suite tests; tests that need it set
    # it themselves.
    monkeypatch.delenv("RENFORGE_ISOLATION", raising=False)
