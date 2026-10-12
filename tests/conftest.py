"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_renforge_state(tmp_path_factory, monkeypatch):
    # RenForge keeps per-project state outside the game; never use the real one.
    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path_factory.mktemp("renforge-state")))
