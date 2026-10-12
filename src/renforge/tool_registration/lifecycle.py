"""Game lifecycle and launch MCP tools."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

TOOL_NAMES = (
    "renforge_launch",
    "renforge_launch_status",
    "renforge_jump",
    "renforge_new_game",
    "renforge_stop",
    "renforge_session",
)

SESSION_ACTIONS = ("launch", "status", "save", "load", "list", "stop")


def build_wrappers(context):
    live = context.live
    _log_tool_call = context.log_tool_call

    def _launch_game(
        project_path: str,
        *,
        version: str = "stable",
        warp: str | None = None,
        editor: bool = True,
        display: str = "auto",
        audio: str = "auto",
        savedir: str | None = None,
        persistent: str | None = None,
        cleanup_on_stop: bool = True,
        timeout: float | None = None,
        session: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
        home: str | None = None,
        preferences: str | None = None,
    ) -> dict:
        from ..dashboard_client import (
            launch_game as launch_via_dashboard,
            stop_game as stop_via_dashboard,
        )
        from ..save_isolation import apply_launch_isolation_defaults

        canonical_editor = bool(editor)
        session_cfg = dict(session or {})
        isolation_defaults = apply_launch_isolation_defaults(
            savedir=session_cfg.get("savedir", savedir),
            home=session_cfg.get("home", home),
            persistent=session_cfg.get("persistent", persistent),
            preferences=session_cfg.get("preferences", preferences),
        )
        effective_savedir = isolation_defaults["savedir"]
        effective_home = isolation_defaults["home"]
        effective_persistent = isolation_defaults["persistent"]
        effective_preferences = isolation_defaults["preferences"]
        effective_cleanup = (
            session_cfg["cleanup_on_stop"]
            if isinstance(session_cfg.get("cleanup_on_stop"), bool)
            else cleanup_on_stop
        )
        if cancel_event is not None and cancel_event.is_set():
            return live.cancelled_launch_result(phase="detecting_environment")

        # None = no matching dashboard. Any dict (including failure) is final:
        # never fall back to a local launch after a contacted dashboard errors.
        delegated = launch_via_dashboard(
            project_path,
            version=version,
            warp=warp,
            editor=canonical_editor,
            display=display,
            audio=audio,
            savedir=effective_savedir,
            persistent=effective_persistent,
            cleanup_on_stop=bool(effective_cleanup),
            timeout=timeout,
            home=effective_home,
            preferences=effective_preferences,
        )
        if delegated is not None:
            if cancel_event is not None and cancel_event.is_set():
                stopped = stop_via_dashboard(project_path)
                if stopped is None:
                    message = (
                        "The dashboard launch was cancelled, but its owning "
                        "dashboard is unavailable to stop it."
                    )
                    return {
                        "ok": False,
                        "ready": False,
                        "code": "DASHBOARD_STOP_UNAVAILABLE",
                        "phase": "stopping_cancelled_dashboard_launch",
                        "message": message,
                        "error": message,
                        "launch_cancel_requested": True,
                    }
                if not stopped.get("ok"):
                    return {
                        **stopped,
                        "ready": False,
                        "launch_cancel_requested": True,
                    }
                return live.cancelled_launch_result(phase="starting_renpy")
            return delegated
        if cancel_event is not None and cancel_event.is_set():
            stopped = live.stop_external_game(project_path)
            if not stopped.get("ok"):
                return {
                    **stopped,
                    "ready": False,
                    "launch_cancel_requested": True,
                }
            return live.cancelled_launch_result(phase="starting_renpy")
        return live.launch_game(
            project_path,
            version=version,
            warp=warp,
            editor=canonical_editor,
            display=display,
            audio=audio,
            savedir=effective_savedir,
            persistent=effective_persistent,
            cleanup_on_stop=bool(effective_cleanup),
            timeout=timeout,
            session=session,
            cancel_event=cancel_event,
            home=effective_home,
            preferences=effective_preferences,
        )


    def _stop_game(project_path: str) -> dict:
        from ..dashboard_client import stop_game as stop_via_dashboard

        delegated = stop_via_dashboard(project_path)
        # Contacted dashboard failure is final; None means no dashboard.
        return delegated if delegated is not None else live.stop_game(project_path)


    def _launch_status(project_path: str) -> dict:
        from ..dashboard_client import launch_status as status_via_dashboard

        delegated = status_via_dashboard(project_path)
        return delegated if delegated is not None else live.launch_status(project_path)


    def _start_launch(project_path: str, **kwargs: Any) -> dict:
        requested_editor = bool(kwargs.get("editor", True))
        kwargs["editor"] = requested_editor

        def _launch(project_root: Path, cancel_event: threading.Event) -> dict:
            return _launch_game(
                str(project_root),
                cancel_event=cancel_event,
                **kwargs,
            )

        return live.start_launch(project_path, _launch, editor=requested_editor)


    def renforge_launch(
        project_path: str,
        warp: str = "",
        version: str = "stable",
        editor: bool = True,
        display: str = "auto",
        audio: str = "auto",
        savedir: str = "auto",
        persistent: str = "auto",
        cleanup_on_stop: bool = True,
        timeout: float = 0,
        home: str = "auto",
        preferences: str = "auto",
        authorize: bool = False,
    ) -> dict:
        """Launch or reuse a game with the Live Editor enabled by default.

        Pass ``editor=False`` to launch intentionally without the visual editor.

        After launch, poll ``renforge_launch_status`` until ready, then observe
        with a fresh ``renforge_screenshot`` or ``renforge_scene_tree`` before
        any click. Use ``renforge_click_at`` or ``renforge_click_element`` with
        a current ``frame_id`` guard; verify the result, then ``renforge_stop``.
        See docs/LIVE_EDITOR.md. Optional ``warp`` is a Ren'Py file:line target.

        The call waits at most 20 seconds for readiness, then returns
        ``status="starting"`` while startup continues in the background. Poll
        ``renforge_launch_status`` until it reports ``ready`` or ``failed``.
        display/audio default to auto. Saves, preferences, and HOME default to
        isolated temporary locations so this session does not read or write the
        user's normal Ren'Py state. The ready payload includes ``session_id``
        and an ``isolation`` object. Pass savedir='existing' (and home='existing'
        if needed) to use the user's files, or set RENFORGE_ISOLATION=existing
        as a server-wide default; that requires authorize=true when
        RENFORGE_POLICY=enforce. timeout controls the background startup
        deadline, not the MCP call (default 180 s, or RENFORGE_STARTUP_TIMEOUT).
        """
        kwargs: dict[str, Any] = {
            "version": version,
            "warp": warp or None,
            "editor": editor,
            "display": display or "auto",
            "audio": audio or "auto",
            "persistent": persistent,
            "cleanup_on_stop": cleanup_on_stop,
            "savedir": savedir,
            "home": home,
            "preferences": preferences,
        }
        if timeout and timeout > 0:
            kwargs["timeout"] = float(timeout)
        return _log_tool_call(
            name="renforge_launch",
            params={
                "project_path": project_path,
                "warp": warp,
                "version": version,
                "editor": editor,
                "display": display,
                "audio": audio,
                "savedir": savedir,
                "persistent": persistent,
                "cleanup_on_stop": cleanup_on_stop,
                "timeout": timeout,
                "home": home,
                "preferences": preferences,
                "authorize": authorize,
            },
            project_root=project_path,
            fn=_start_launch,
            args=(project_path,),
            kwargs=kwargs,
        )


    def renforge_launch_status(project_path: str) -> dict:
        """Return starting, ready, failed, or idle for a background launch."""
        return _log_tool_call(
            name="renforge_launch_status",
            params={"project_path": project_path},
            project_root=project_path,
            fn=_launch_status,
            args=(project_path,),
            kwargs={},
        )


    def renforge_jump(
        project_path: str,
        target: str,
        version: str = "stable",
        savedir: str = "auto",
        persistent: str = "auto",
        home: str = "auto",
        preferences: str = "auto",
        authorize: bool = False,
    ) -> dict:
        """Restart at a label or file:line; poll launch status when still starting.

        Isolation knobs match ``renforge_launch``. Exposing the user save tree
        requires authorize=true when RENFORGE_POLICY=enforce.
        """
        from ..navigation import resolve_warp_target

        def _jump() -> dict:
            resolved = resolve_warp_target(project_path, target)
            if not resolved.get("ok"):
                return resolved
            return _start_launch(
                project_path,
                version=version,
                warp=str(resolved["target"]),
                savedir=savedir,
                persistent=persistent,
                home=home,
                preferences=preferences,
            )

        return _log_tool_call(
            name="renforge_jump",
            params={
                "project_path": project_path,
                "target": target,
                "version": version,
                "savedir": savedir,
                "persistent": persistent,
                "home": home,
                "preferences": preferences,
                "authorize": authorize,
            },
            project_root=project_path,
            fn=_jump,
            args=(),
            kwargs={},
        )


    def renforge_new_game(
        project_path: str,
        version: str = "stable",
        savedir: str = "auto",
        persistent: str = "auto",
        home: str = "auto",
        preferences: str = "auto",
        authorize: bool = False,
    ) -> dict:
        """Start at the ``start`` label; poll launch status when still starting.

        Isolation knobs match ``renforge_launch``. Exposing the user save tree
        requires authorize=true when RENFORGE_POLICY=enforce.
        """
        from ..navigation import resolve_warp_target

        def _new_game() -> dict:
            resolved = resolve_warp_target(project_path, "start")
            if not resolved.get("ok"):
                return resolved
            return _start_launch(
                project_path,
                version=version,
                warp=str(resolved["target"]),
                savedir=savedir,
                persistent=persistent,
                home=home,
                preferences=preferences,
            )

        return _log_tool_call(
            name="renforge_new_game",
            params={
                "project_path": project_path,
                "version": version,
                "savedir": savedir,
                "persistent": persistent,
                "home": home,
                "preferences": preferences,
                "authorize": authorize,
            },
            project_root=project_path,
            fn=_new_game,
            args=(),
            kwargs={},
        )


    def renforge_stop(project_path: str) -> dict:
        """Stop a running game or cancel its in-progress launch, then clean up."""
        return _log_tool_call(
            name="renforge_stop",
            params={"project_path": project_path},
            project_root=project_path,
            fn=_stop_game,
            args=(project_path,),
            kwargs={},
        )


    def renforge_session(project_path: str, action: str, slot: str | None = None,
                         authorize: bool = False) -> dict:
        """Start, check, save, load, list saves of, or stop a play session.

        The canonical lifecycle for agents. ``launch`` uses agent-mode
        defaults: saves and preferences in the project's RenForge state
        folder (never the player's, and kept across launches), an isolated
        HOME, no Live Editor. Poll ``status`` until ready.
        """

        def _session() -> dict:
            if action not in SESSION_ACTIONS:
                return {"ok": False, "error": "action must be one of: " + ", ".join(SESSION_ACTIONS)}
            if action == "launch":
                # Saves stay apart from the player's own but survive relaunches.
                from ..state import project_state_dir
                saves = project_state_dir(project_path) / "saves"
                return _start_launch(project_path, editor=False, savedir=str(saves))
            if action == "status":
                return _launch_status(project_path)
            if action == "stop":
                return _stop_game(project_path)
            if action in ("save", "load") and not (isinstance(slot, str) and slot.strip()):
                return {"ok": False, "error": f"{action} needs a slot name"}
            return live.saves(project_path, action, slot=slot)

        return _log_tool_call(
            name="renforge_session",
            params={"project_path": project_path, "action": action, "slot": slot,
                    "authorize": authorize},
            project_root=project_path,
            fn=_session,
            args=(),
            kwargs={},
        )


    return {name: value for name, value in locals().items() if name in TOOL_NAMES}


def register(registrar, wrappers) -> None:
    registrar.register_many(wrappers, TOOL_NAMES)
