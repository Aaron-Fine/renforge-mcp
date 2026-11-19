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

SESSION_ACTIONS = ("launch", "status", "save", "load", "list", "import", "stop")

_LANGUAGE_RESULT_KEYS = ("requested_language", "active_language", "language_applied")


def _annotate_language_result(
    result: dict[str, Any],
    language: str | None,
    warning: str | None = None,
) -> dict[str, Any]:
    """Keep language fields honest for delegated dashboard launches.

    An older dashboard ignores the unknown ``language`` key and returns no
    language fields. Without this note the launch would look as if the language
    had been applied. A successful launch that used the default savedir also
    needs the persistence warning, because Ren'Py stores the language there.
    """
    annotated = dict(result)
    reported_language = any(key in annotated for key in _LANGUAGE_RESULT_KEYS)
    if language is not None and annotated.get("ok") and not reported_language:
        note = (
            "The dashboard did not report language support, so the requested "
            f"language {language!r} was not confirmed. Upgrade the dashboard or "
            "launch without a language."
        )
        annotated["requested_language"] = language
        annotated["language_applied"] = False
        message = annotated.get("message")
        annotated["message"] = f"{message} {note}" if isinstance(message, str) and message else note
    if language is not None and not annotated.get("ok"):
        annotated["language_applied"] = False
    if (
        warning is not None
        and annotated.get("ok")
        and not annotated.get("already_running")
        and reported_language
    ):
        annotated.setdefault("warning", warning)
    return annotated


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
        language: str | None = None,
        session: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
        home: str | None = None,
        preferences: str | None = None,
        start_label: str | None = None,
    ) -> dict:
        from ..dashboard_client import (
            launch_game as launch_via_dashboard,
            stop_game as stop_via_dashboard,
        )
        from ..save_isolation import (
            apply_launch_isolation_defaults,
            configured_isolation_mode,
            isolation_knobs_unset,
        )

        canonical_editor = bool(editor)
        session_cfg = dict(session or {})
        knobs = {
            "savedir": session_cfg.get("savedir", savedir),
            "home": session_cfg.get("home", home),
            "persistent": session_cfg.get("persistent", persistent),
            "preferences": session_cfg.get("preferences", preferences),
        }
        isolation_defaults = apply_launch_isolation_defaults(**knobs)
        effective_savedir = isolation_defaults["savedir"]
        effective_home = isolation_defaults["home"]
        effective_persistent = isolation_defaults["persistent"]
        effective_preferences = isolation_defaults["preferences"]
        # A relaunch (warp) with every knob auto is resolved by the process
        # that owns the running game, so an isolated game's jump stays
        # isolated there instead of being sent the player's saves from here.
        # When this server's RENFORGE_ISOLATION already isolates, resolve here
        # instead: the owner may strengthen isolation, never drop it.
        owner_resolves = (
            warp is not None
            and isolation_knobs_unset(*knobs.values())
            and configured_isolation_mode() != "temporary"
        )
        if owner_resolves:
            effective_savedir = effective_home = None
            effective_persistent = effective_preferences = None
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
            start_label=start_label,
            language=language,
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
            warning_savedir = effective_savedir
            if owner_resolves:
                # The dashboard chose; its isolation report says what it chose.
                reported = delegated.get("isolation")
                warning_savedir = (
                    reported.get("savedir_mode")
                    if isinstance(reported, dict) and reported.get("savedir_mode")
                    else isolation_defaults["savedir"]
                )
            warning = live.language_persistence_warning(
                language,
                warning_savedir if isinstance(warning_savedir, str) else None,
            )
            return _annotate_language_result(delegated, language, warning)
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
            language=language,
            session=session,
            cancel_event=cancel_event,
            home=effective_home,
            preferences=effective_preferences,
            start_label=start_label,
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

        requirements = live.launch_requirements(
            project_path,
            **{field: kwargs.get(field) for field in ("warp", "savedir", "home", "persistent", "preferences")})
        return live.start_launch(
            project_path,
            _launch,
            editor=requested_editor,
            language=kwargs.get("language"),
            requirements=requirements,
        )


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
        language: str | None = None,
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
        display/audio default to auto. Saves and HOME default to the user's own,
        as before; pass savedir='temporary' (or set RENFORGE_ISOLATION=temporary
        server-wide) to isolate saves, persistent data, preferences, and HOME in
        a disposable session folder. The ready payload includes ``session_id``
        and an ``isolation`` object. Under RENFORGE_POLICY=enforce a launch
        needs authorize=true unless its saves and HOME both stay in
        RenForge's own folders (savedir='temporary' with home omitted or
        'temporary', or state-folder paths), so by default one with no
        isolation knobs needs it; pass savedir='temporary' instead. The rule
        is stated once in tool_definitions._LAUNCH_POLICY. timeout controls the
        background startup deadline, not the MCP call (default 180 s, or
        RENFORGE_STARTUP_TIMEOUT).
        ``language`` sets ``RENPY_LANGUAGE`` for a fresh game process. Ren'Py
        persists it as ``preferences.language``, so this overrides the stored
        preference, not just one session; for verification use
        ``savedir="temporary"`` (and ``persistent="empty"``, which needs an
        isolated savedir) or restore the language afterward. It cannot change an
        already-running session. Only this tool accepts ``language``: a later
        ``renforge_jump``/``renforge_new_game`` or dashboard Warp relaunch
        starts without it and returns to the stored preference.
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
        if language is not None:
            kwargs["language"] = language
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
                "language": language,
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

        Isolation knobs and the RENFORGE_POLICY=enforce rule match
        ``renforge_launch``; the policy judges these knobs, not the game
        being replaced, so with no knobs this needs authorize=true unless
        RENFORGE_ISOLATION=temporary. Pass savedir='temporary' instead.
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

        Isolation knobs and the RENFORGE_POLICY=enforce rule match
        ``renforge_launch``; the policy judges these knobs, not the game
        being replaced, so with no knobs this needs authorize=true unless
        RENFORGE_ISOLATION=temporary. Pass savedir='temporary' instead.
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
                         label: str | None = None, authorize: bool = False) -> dict:
        """Start, check, save, load, list saves of, or stop a play session.

        The canonical lifecycle for agents. ``launch`` uses agent-mode
        defaults: saves and preferences in the project's RenForge state
        folder (never the player's, and kept across launches), an isolated
        HOME, no Live Editor. Poll ``status`` until ready. ``save``, ``load``
        and ``list`` refuse a game whose saves or HOME are outside RenForge's
        own folders (``SESSION_NOT_ISOLATED``).
        """

        def _session() -> dict:
            if action not in SESSION_ACTIONS:
                return {"ok": False, "error": "action must be one of: " + ", ".join(SESSION_ACTIONS)}
            if label is not None and action != "launch":
                return {"ok": False, "error": "label is only for action=launch"}
            if label is not None and not (isinstance(label, str) and label.strip()):
                return {"ok": False, "error": "label must be a label name"}
            if action == "launch":
                # Saves stay apart from the player's own but survive relaunches.
                saves = live.session_saves_dir(project_path)
                # HOME is always the session's own, even when the server's
                # RENFORGE_ISOLATION=existing would default it to the player's:
                # the policy classifies this launch as isolated.
                result = _start_launch(project_path, editor=False, savedir=str(saves), home="temporary",
                                       start_label=label.strip() if label else None)
                # Only a game this call starts goes to the label: a reused one
                # stays where it is, and a refused launch starts nothing.
                if label and isinstance(result, dict) and result.get("ok") and not result.get("already_running"):
                    result["start_label"] = label.strip()
                return result
            if action == "status":
                return _launch_status(project_path)
            if action == "stop":
                return _stop_game(project_path)
            if action in ("save", "load", "import") and not (isinstance(slot, str) and slot.strip()):
                return {"ok": False, "error": f"{action} needs a slot name"}
            if action == "import":
                return _import(slot.strip())
            # Only the session's own saves: never a game on the player's saves.
            result = live.saves(project_path, action, slot=slot, renforge_owned_only=True)
            if action == "load" and isinstance(result, dict) and result.get("ok"):
                result = _after_load(result)
            # A failed list (refused, or no game) lists nothing, so it says
            # nothing about anyone's slots either.
            if action == "list" and isinstance(result, dict) and result.get("ok"):
                save_name = result.pop("save_directory", None)
                if _may_read_player_saves():
                    result["player_slots"] = _player_slots(save_name)
                else:
                    result["player_slots_hidden"] = (
                        "slots are this session's own saves; the player's own slots are "
                        "listed only with authorize=true under RENFORGE_POLICY=enforce")
            return result

        def _may_read_player_saves() -> bool:
            """Whether this call may read the player's saves, as renforge_saves list_user may."""
            from .. import policy

            return policy.evaluate("renforge_saves", {"action": "list_user", "project_path": project_path,
                                                      "authorize": authorize}).allowed

        def _save_name() -> str | None:
            """The game's config.save_directory, from the running game when there is one."""
            listed = live.saves(project_path, "list")
            name = listed.get("save_directory") if isinstance(listed, dict) else None
            return name if isinstance(name, str) and name else None

        def _player_slots(save_name: str | None) -> list[dict]:
            """The player's own save slots for this game, which import can copy in."""
            from ..save_isolation import list_host_slots

            listed = list_host_slots(project_path, save_name=save_name)
            return [{key: item[key] for key in ("name", "extra_info") if item.get(key)}
                    for item in listed.get("slots") or []]

        def _import(name: str) -> dict:
            """Copy one of the player's slots into the saves a load will read; the player's file is only read.

            That is the running game's save folder when it reports one, else
            the session's saves folder, which the next launch uses.
            """
            from ..save_isolation import import_host_slots
            from ..util.files import ensure_nofollow_directory

            saves = live.session_saves_dir(project_path)
            running = live.running_game_savedir(project_path)
            elsewhere = running is not None and not _same_folder(Path(running), saves)
            if elsewhere and not live.owns_session(project_path):
                return _saves_elsewhere(running)
            save_name = _save_name()
            if elsewhere:
                replaced = _has_slot(Path(running), name)
                result = live.import_into_owned_session(project_path, slot=name, save_name=save_name)
                if result.get("code") == "SAVE_IMPORT_NOT_ISOLATED":
                    return _saves_elsewhere(running)
            else:
                replaced = _has_slot(saves, name)
                # The import creates only the saves folder itself; the state
                # folder is RenForge's own (and already resolved), so make it here.
                ensure_nofollow_directory(saves.parent)
                result = import_host_slots(project_path, saves, slot=name, save_name=save_name)
            touched = list(result.get("files_touched") or [])
            if not result.get("ok"):
                if result.get("code") in (None, "NO_MATCHING_SAVE_SLOTS"):
                    return {"ok": False, "code": "NO_MATCHING_SAVE_SLOTS", "files_touched": touched,
                            "error": f"the player has no save slot {name!r}; list shows player_slots"}
                return {**result, "files_touched": touched}
            return {"ok": True, "slot": name, "replaced": replaced, "files_touched": touched,
                    "next": ("load this slot to play from it. Ren'Py asks whether to trust a save made on "
                             "another device: choose Yes for the save, then No for the device, which loads "
                             "this save without trusting others.")}

        def _saves_elsewhere(savedir: str) -> dict:
            message = (f"the running game keeps its saves in {savedir}, which this server did not create, so a "
                       "load could not read an imported slot; stop the game and launch with "
                       "renforge_session(action=\"launch\"), then import")
            return {"ok": False, "code": "SESSION_SAVES_ELSEWHERE", "files_touched": [],
                    "message": message, "error": message}

        def _has_slot(folder: Path, name: str) -> bool:
            """Whether *folder* already holds a save file of slot *name*."""
            from ..save_isolation import SLOT_FILE_RE

            try:
                entries = list(folder.iterdir())
            except OSError:
                return False
            return any((match := SLOT_FILE_RE.match(entry.name)) is not None
                       and match["slot"] == name and not match["json"] for entry in entries)

        def _same_folder(first: Path, second: Path) -> bool:
            try:
                return first.resolve() == second.resolve()
            except OSError:
                return first == second

        def _after_load(result: dict) -> dict:
            """Return the restored game's observation, so the next act can use it.

            The bridge replies before the engine has restarted into the save, so
            its ``restored_label`` names the label from before the load.
            """
            import time

            from ..compact import compact_observation

            result.pop("restored_label", None)
            result.pop("restarting", None)
            result.pop("ended_interaction", None)
            observation = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                observation = live.observe(project_path, screenshot=False)
                if isinstance(observation, dict) and observation.get("stable") and observation.get("observation_id"):
                    break
                time.sleep(0.25)
            if isinstance(observation, dict) and observation.get("observation_id"):
                result["observation"] = compact_observation(observation)
            return result

        return _log_tool_call(
            name="renforge_session",
            params={"project_path": project_path, "action": action, "slot": slot,
                    "label": label, "authorize": authorize},
            project_root=project_path,
            fn=_session,
            args=(),
            kwargs={},
        )


    return {name: value for name, value in locals().items() if name in TOOL_NAMES}


def register(registrar, wrappers) -> None:
    registrar.register_many(wrappers, TOOL_NAMES)
