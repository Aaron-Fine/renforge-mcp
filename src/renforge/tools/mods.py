"""Mod tool dispatcher: file operations plus a live reload check."""

from __future__ import annotations

import time
from typing import Any

from .. import mods
from .live import control, get_errors, observe

ACTIONS = ("write", "list", "lint", "reload", "remove", "package", "guide")
_ACTIONS_WITHOUT_NAME = ("list", "guide")
RELOAD_POLL_INTERVAL = 0.2
RELOAD_TIMEOUT = 15.0


def _error_reply(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error}


def _errors_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """True when the error reply shows events or crash-file tails not seen before."""
    if after.get("events"):
        return True
    return list(after.get("files") or []) != list(before.get("files") or [])


def _reload(project_path: str, name: str) -> dict[str, Any]:
    baseline = get_errors(project_path)
    baseline = baseline if isinstance(baseline, dict) else {}
    cursor = baseline.get("cursor", 0)
    if not isinstance(cursor, int):
        cursor = 0

    reload_reply = control(project_path, "reload_script")
    if not isinstance(reload_reply, dict) or not reload_reply.get("ok"):
        return {"ok": False, "reload": reload_reply, "observation": None, "errors": None}

    observation: dict[str, Any] | None = None
    deadline = time.monotonic() + RELOAD_TIMEOUT
    while True:
        observation = observe(project_path, screenshot=False)
        if isinstance(observation, dict) and observation.get("stable") is True:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(RELOAD_POLL_INTERVAL)

    errors = get_errors(project_path, since=cursor)
    if not isinstance(errors, dict):
        errors = _error_reply("get_errors returned a non-object result")
    ok = bool(reload_reply.get("ok")) and errors.get("ok") is True and not _errors_changed(baseline, errors)
    return {
        "ok": ok,
        "reload": reload_reply,
        "observation": observation,
        "errors": errors,
    }


def mod(
    project_path: str,
    action: str,
    name: str | None = None,
    path: str | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """Dispatch one mod action. Errors come back as ``{"ok": False, "error": ...}``."""
    if action not in ACTIONS:
        return _error_reply(f"action must be one of {', '.join(ACTIONS)}")
    if action not in _ACTIONS_WITHOUT_NAME and not isinstance(name, str):
        return _error_reply(f"name is required for action '{action}'")
    if action == "write":
        if not isinstance(path, str) or not path:
            return _error_reply("path is required for action 'write'")
        if not isinstance(content, str):
            return _error_reply("content is required for action 'write'")

    try:
        if action == "guide":
            return {"ok": True, "guide": mods.GUIDE}
        if action == "list":
            return mods.list_mods(project_path)
        if action == "write":
            return mods.write_file(project_path, name, path, content)
        if action == "lint":
            return mods.lint(project_path, name)
        if action == "reload":
            mods._check_name(name)
            return _reload(project_path, name)
        if action == "remove":
            return mods.remove(project_path, name)
        return mods.package(project_path, name)
    except ValueError as exc:
        return _error_reply(str(exc))
