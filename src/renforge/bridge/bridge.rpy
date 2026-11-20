init python:
    # RenForge in-game bridge.
    #
    # Injected temporarily into <project>/game/ by the launcher and removed
    # afterwards. Opens a localhost TCP server (token-authenticated) so an
    # external client can inspect and drive the running game.
    #
    # Threading model: the socket listener runs on a background thread, but any
    # call into the Ren'Py API MUST happen on the main thread. Each request is
    # therefore handed to the main thread through a queue and executed inside a
    # `config.periodic_callbacks` drain; the listener thread blocks on a
    # per-request Event until the result is ready, then writes the reply.
    #
    # Configuration comes from the environment:
    #   RENFORGE_BRIDGE_TOKEN        required 64-lowercase-hex auth token
    #   RENFORGE_BRIDGE_SESSION_ID   required 32-lowercase-hex session id
    #   RENFORGE_BRIDGE_PROJECT_ROOT required canonical absolute project root
    #   RENFORGE_BRIDGE_PORT         default 0 (an ephemeral port is chosen)
    #   RENFORGE_BRIDGE_CONTROL_DIR  private control folder, outside the game
    #   RENFORGE_BRIDGE_EVAL         unset or "on" lets eval and set_var run
    #                                code; any other value switches them off
    # The launcher reserves starting metadata at
    #   <control dir>/bridge.json
    # Before serving, the bridge validates that record and publishes ready.

    import base64
    import builtins
    import collections
    import hashlib
    import hmac as _hmac
    import json
    import os
    import queue
    import re
    import socket
    import struct
    import sys
    import threading
    # `time` is a store field. Games use that name for their own clock, which
    # replaces the module before a label callback runs. `math` is aliased
    # for the same reason.
    import time as _renforge_time
    import math as _renforge_math
    import types

    try:
        import pygame_sdl2 as pygame
    except Exception:
        # A real Ren'Py SDK always provides pygame_sdl2. Keeping the import
        # optional lets the bridge's non-engine RPC tests load this file with a
        # minimal fake runtime; input commands report a clear error if events
        # cannot be posted.
        pygame = None

    # Keep runtime state off renpy.store / rollback. `init python` top-level
    # names become store fields; a Queue/lock inside the bridge is not picklable
    # and would break QuickSave. A dedicated sys.modules entry is never saved.
    if "_renforge_runtime" not in sys.modules:
        sys.modules["_renforge_runtime"] = types.ModuleType("_renforge_runtime")
    _renforge_runtime = sys.modules["_renforge_runtime"]
    if not hasattr(_renforge_runtime, "bridge"):
        _renforge_runtime.bridge = None

    # Replaced with the canonical module source by launcher.bridge_payload().
    # Execute in a normal module: game store names and rollback cannot replace
    # its builtins, imports, or predicate globals.
    _renforge_runtime.interaction = types.ModuleType("_renforge_interaction")
    sys.modules["_renforge_interaction"] = _renforge_runtime.interaction
    exec(compile("__RENFORGE_INTERACTION_SOURCE__", "renforge/interaction.py", "exec"),
         _renforge_runtime.interaction.__dict__)

    # The plain object: `object` in init python is Ren'Py's RevertableObject,
    # whose changes go into the rollback log, and saving would then pickle the
    # bridge's queue and locks ("cannot pickle
    # '_thread.lock' object ... requests.mutex").
    class _RenforgeRequest(builtins.object):
        # NB: no __slots__ — Ren'Py forbids slotted classes in init python
        # (they are incompatible with its rollback machinery).
        def __init__(self, command, payload):
            self.command = command
            self.payload = payload
            self.event = threading.Event()
            self.result = None
            self.error = None

    class _RenforgeBridge(builtins.object):
        def __init__(self, host, port, token, project_root, session_id):
            self.host = host
            self.port = port
            self.token = token
            self.project_root = project_root
            self.session_id = session_id
            self.requests = queue.Queue()
            self.stop = threading.Event()
            self.thread = None
            self.current_label = None
            # Pushed events buffer (main-thread only): dialogue lines, label
            # entries and exceptions. Clients retrieve them via `poll_events`.
            self.events = collections.deque(maxlen=1000)
            self.event_seq = 0
            self.last_say = None
            self.prev_exception_handler = None
            # Correlation id for the command currently executing on the main
            # thread; business events inherit it so agents can attribute effects.
            self.current_correlation_id = None
            self.prev_skipping = None
            self.prev_afm = None
            self.prev_history_index = None
            self.interaction_counter = 0
            # ui.interact generation for observe/act. This is not interaction_counter,
            # which is only a correlation id for business events.
            self.observe_generation = 0
            self.last_who = None
            self.held_keys = set()
            self.say_id = 0
            self.say_recorded = False
            self._skip_reason_hint = None
            self.progression = None
            self.pointer_click = None
            self.current_request = None
            self.nested_reply_request = None
            self.say_ended_generation = None
            self.pending_ending = None
            self.ending = None
            self.ending_counter = 0

        def push_event(self, kind, data):
            self.event_seq += 1
            record = {
                "seq": self.event_seq,
                "type": kind,
                "timestamp": _renforge_time.time(),
            }
            if self.current_correlation_id is not None:
                record["correlation_id"] = self.current_correlation_id
            if data:
                record.update(data)
            self.events.append(record)
            return record

    def _renforge_type_label(value):
        try:
            name = type(value).__name__
        except Exception:
            return "object"
        if not isinstance(name, str) or not name:
            return "object"
        if len(name) > 80:
            return name[:80]
        return name

    def _renforge_jsonable(value):
        """Best-effort conversion of a Python value to something JSON-safe.

        Does not call user conversion hooks such as ``__repr__`` or ``__str__``.
        """
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple)):
            return [_renforge_jsonable(v) for v in value]
        if isinstance(value, builtins.dict):
            out = {}
            for k, v in value.items():
                if k is None or isinstance(k, (bool, int, float, str)):
                    key = str(k)
                else:
                    key = "<%s>" % _renforge_type_label(k)
                out[key] = _renforge_jsonable(v)
            return out
        return "<%s>" % _renforge_type_label(value)

    def _renforge_store_snapshot():
        snapshot = {}
        for name, value in list(vars(renpy.store).items()):
            if name.startswith("_"):
                continue
            if callable(value):
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                continue
            snapshot[name] = value
        return snapshot

    _RENFORGE_STATE_INCLUDES = ("metrics", "audio")

    def _renforge_state_includes(payload):
        """Validate the optional, compact sections requested with get_state."""
        payload = payload or {}
        if "include" not in payload or payload.get("include") is None:
            return [], None
        include = payload.get("include")
        if isinstance(include, str) or not isinstance(include, (builtins.list, tuple)):
            return [], "include must be a list containing only: metrics, audio"
        unknown = [name for name in include if name not in _RENFORGE_STATE_INCLUDES]
        if unknown:
            return [], "include contains unsupported values: %s (supported: metrics, audio)" % ", ".join(str(name) for name in unknown)
        return list(builtins.dict.fromkeys(include)), None

    def _renforge_size(value):
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            return None
        try:
            return {"width": int(value[0]), "height": int(value[1])}
        except (TypeError, ValueError, OverflowError):
            return None

    def _renforge_physical_size():
        get_size = getattr(renpy, "get_physical_size", None)
        if callable(get_size):
            try:
                size = _renforge_size(get_size())
                if size is not None:
                    return size
            except Exception:
                pass

        draw = getattr(getattr(renpy, "display", None), "draw", None)
        get_size = getattr(draw, "get_physical_size", None)
        if callable(get_size):
            try:
                size = _renforge_size(get_size())
                if size is not None:
                    return size
            except Exception:
                pass

        preferences = getattr(getattr(renpy, "game", None), "preferences", None)
        size = _renforge_size(getattr(preferences, "physical_size", None))
        if size is not None:
            return size

        config = getattr(renpy, "config", None)
        width = getattr(config, "physical_width", None)
        height = getattr(config, "physical_height", None)
        if width and height:
            return _renforge_size((width, height))
        return None

    def _renforge_h_get_metrics(payload):
        """Return inexpensive frame, image-cache, and window diagnostics."""
        interface = getattr(getattr(renpy, "display", None), "interface", None)
        frame_times = list(getattr(interface, "frame_times", None) or [])
        intervals = []
        for previous, current in zip(frame_times, frame_times[1:]):
            try:
                delta = float(current) - float(previous)
            except (TypeError, ValueError):
                continue
            if delta > 0:
                intervals.append(delta)

        fps = 0.0
        if intervals:
            recent = intervals[-10:]
            average = sum(recent) / len(recent)
            if average > 0:
                fps = 1.0 / average

        render_time_ms = None
        get_render_time = getattr(renpy, "get_render_time", None)
        if callable(get_render_time):
            try:
                render_time_ms = float(get_render_time()) * 1000.0
            except (TypeError, ValueError, OverflowError):
                render_time_ms = None
        if render_time_ms is None:
            render_time_ms = (intervals[-1] * 1000.0) if intervals else 0.0

        image_cache_size = 0
        image_cache_entries = 0
        image_cache_limit = None
        image_module = getattr(getattr(renpy, "display", None), "im", None)
        image_cache = getattr(image_module, "cache", None)
        if image_cache is not None:
            get_total_size = getattr(image_cache, "get_total_size", None)
            try:
                if callable(get_total_size):
                    image_cache_size = get_total_size()
                else:
                    image_cache_size = getattr(image_cache, "cache_size", 0)
                image_cache_entries = len(getattr(image_cache, "cache", {}) or {})
                image_cache_limit = getattr(image_cache, "cache_limit", None)
            except Exception:
                image_cache_size = 0

        config = getattr(renpy, "config", None)
        logical = _renforge_size((
            getattr(config, "screen_width", None),
            getattr(config, "screen_height", None),
        ))
        return {
            "render_time_ms": _renforge_jsonable(render_time_ms),
            "fps": _renforge_jsonable(fps),
            "image_cache_size": _renforge_jsonable(image_cache_size),
            "image_cache_entries": _renforge_jsonable(image_cache_entries),
            "image_cache_limit": _renforge_jsonable(image_cache_limit),
            "window": {
                "logical": logical,
                "physical": _renforge_physical_size(),
            },
        }

    def _renforge_audio_channel_names():
        names = []
        audio = getattr(getattr(renpy, "audio", None), "audio", None)
        for channel in list(getattr(audio, "all_channels", None) or []):
            name = getattr(channel, "name", channel)
            if name is not None and str(name) not in names:
                names.append(str(name))
        for name in list((getattr(audio, "channels", None) or {}).keys()):
            if str(name) not in names:
                names.append(str(name))
        if not names:
            names = ["music", "sound", "voice"]
        return names

    def _renforge_audio_value(music, channel, method_name):
        method = getattr(music, method_name, None)
        if not callable(method):
            return None
        try:
            return _renforge_jsonable(method(channel=channel))
        except TypeError:
            try:
                return _renforge_jsonable(method(channel))
            except Exception:
                return None
        except Exception:
            return None

    def _renforge_h_get_audio_state(payload):
        """Return one compact record for every registered audio channel."""
        music = getattr(renpy, "music", None)
        audio = getattr(getattr(renpy, "audio", None), "audio", None)
        channels = getattr(audio, "channels", None) or {}
        result = {}
        for name in _renforge_audio_channel_names():
            channel = channels.get(name)
            playing = _renforge_audio_value(music, name, "get_playing")
            volume = _renforge_audio_value(music, name, "get_volume")
            pause = _renforge_audio_value(music, name, "get_pause")
            if channel is not None:
                if volume is None:
                    volume = _renforge_jsonable(getattr(channel, "actual_volume", None))
                    if volume is None:
                        volume = _renforge_jsonable(getattr(channel, "chan_volume", None))
                if pause is None:
                    context = getattr(channel, "context", None)
                    pause = _renforge_jsonable(getattr(context, "pause", None))
            result[name] = {
                "playing": playing,
                "volume": volume,
                "pause": pause,
            }
        return result

    def _renforge_screen_display_name(displayable, fallback):
        raw_name = getattr(displayable, "screen_name", None)
        if isinstance(raw_name, (list, tuple)):
            raw_name = " ".join(str(part) for part in raw_name)
        if raw_name:
            return str(raw_name)
        return fallback

    def _renforge_h_inspect_screen(payload):
        payload = payload or {}
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return {"ok": False, "error": "screen name is required"}
        name = name.strip()
        get_screen = getattr(renpy, "get_screen", None)
        if not callable(get_screen):
            return {"ok": False, "error": "screen inspection is unavailable"}
        try:
            displayable = get_screen(name)
        except Exception as exc:
            return {"ok": False, "error": "could not inspect screen %s: %s" % (name, exc)}
        if displayable is None:
            return {
                "ok": True,
                "active": False,
                "name": name,
                "error": "screen not showing: %s" % name,
            }

        raw_scope = getattr(displayable, "scope", {}) or {}
        try:
            scope_items = raw_scope.items()
        except AttributeError:
            scope_items = []
        scope = {}
        for key, value in scope_items:
            if str(key) in ("_args", "_kwargs", "_scope", "_name", "_debug"):
                continue
            scope[str(key)] = _renforge_jsonable(value)

        raw_args = raw_scope.get("_args", ()) if hasattr(raw_scope, "get") else ()
        raw_kwargs = raw_scope.get("_kwargs", {}) if hasattr(raw_scope, "get") else {}
        if raw_args is None:
            raw_args = ()
        if not isinstance(raw_args, (list, tuple)):
            raw_args = (raw_args,)
        if not isinstance(raw_kwargs, builtins.dict):
            raw_kwargs = {}
        arguments = {
            "args": _renforge_jsonable(list(raw_args)),
            "kwargs": _renforge_jsonable(raw_kwargs),
        }
        return {
            "ok": True,
            "active": True,
            "name": _renforge_screen_display_name(displayable, name),
            "layer": _renforge_jsonable(getattr(displayable, "layer", None)),
            "scope": scope,
            "arguments": arguments,
        }

    def _renforge_selected_store_variables(names):
        """Return a compact map of selected store paths (supports dotted names)."""
        selected = {}
        if not names:
            return selected
        store = renpy.store
        for raw_name in names:
            try:
                name = str(raw_name)
            except Exception:
                continue
            if not name:
                continue
            if "." not in name:
                if hasattr(store, name):
                    try:
                        selected[name] = _renforge_jsonable(getattr(store, name))
                    except Exception:
                        pass
                continue
            # Dotted path: walk attributes from renpy.store / renpy modules.
            parts = name.split(".")
            current = store
            if parts[0] == "config":
                current = renpy.config
                parts = parts[1:]
            elif parts[0] == "_preferences":
                current = getattr(store, "_preferences", None)
                parts = parts[1:]
            ok = current is not None
            for part in parts:
                if not ok:
                    break
                try:
                    current = getattr(current, part)
                except Exception:
                    ok = False
            if ok:
                try:
                    selected[name] = _renforge_jsonable(current)
                except Exception:
                    pass
        return selected

    def _renforge_h_session_info(payload):
        """What a server reusing this game must check: does it run code, where are its saves and HOME."""
        savedir = getattr(renpy.config, "savedir", None)
        home = os.environ.get("USERPROFILE" if os.name == "nt" else "HOME")
        # Ren'Py reads and writes every registered location (game/saves and
        # config.extra_savedirs too), so report them all, or None when the
        # list cannot be read.
        save_locations = None
        try:
            location = getattr(getattr(renpy, "loadsave", None), "location", None)
            directories = [getattr(item, "directory", None) for item in location.locations]
            if all(isinstance(item, str) and item for item in directories):
                save_locations = directories
        except Exception:
            save_locations = None
        return {
            "ok": True,
            "eval_enabled": _renforge_eval_refused() is None,
            "savedir": savedir if isinstance(savedir, str) and savedir else None,
            "home": home or None,
            "save_locations": save_locations,
        }

    def _renforge_h_get_state(payload):
        include, include_error = _renforge_state_includes(payload)
        if include_error is not None:
            return {"ok": False, "error": include_error}
        payload = payload or {}
        profile = payload.get("state_profile") or "full"
        try:
            profile = str(profile).strip().lower()
        except Exception:
            profile = "full"
        if profile not in ("minimal", "interaction", "debug", "full"):
            return {
                "ok": False,
                "error": "state_profile must be one of: minimal, interaction, debug, full",
            }
        try:
            showing = list(renpy.get_showing_tags())
        except Exception:
            showing = []
        try:
            menu_active = renpy.get_screen("choice") is not None
        except Exception:
            menu_active = False
        bridge = _renforge_runtime.bridge
        result = {
            "current_label": bridge.current_label if bridge is not None else None,
            "showing_tags": showing,
            "menu": menu_active,
            "state_profile": profile,
        }
        if bridge is not None and getattr(bridge, "last_say", None):
            result["dialogue"] = bridge.last_say
        try:
            result["skipping"] = _renforge_jsonable(getattr(renpy.config, "skipping", None))
        except Exception:
            pass
        try:
            prefs = getattr(renpy.store, "_preferences", None)
            if prefs is not None:
                result["auto"] = bool(getattr(prefs, "afm_enable", False))
        except Exception:
            pass
        try:
            prefs = getattr(renpy.store, "_preferences", None)
            language = getattr(prefs, "language", None) if prefs is not None else None
            if language is None:
                language = getattr(renpy.config, "language", None)
            result["language"] = _renforge_jsonable(language)
        except Exception:
            result["language"] = None

        extra_vars = payload.get("variables") or payload.get("variable_names") or []
        if isinstance(extra_vars, str):
            extra_vars = [extra_vars]
        if profile == "full":
            result["variables"] = _renforge_store_snapshot()
        elif profile in ("interaction", "debug"):
            names = [
                "config.skipping",
                "_preferences.skip_after_choices",
                "_preferences.skip_unseen",
                "_preferences.afm_enable",
            ]
            if extra_vars:
                names.extend(list(extra_vars))
            selected = _renforge_selected_store_variables(names)
            if selected:
                result["variables"] = selected
        elif extra_vars:
            selected = _renforge_selected_store_variables(list(extra_vars))
            if selected:
                result["variables"] = selected

        if "metrics" in include:
            result["metrics"] = _renforge_h_get_metrics({})
        if "audio" in include:
            result["audio"] = {"channels": _renforge_h_get_audio_state({})}
        return result

    # --- handlers: all run on the MAIN thread -----------------------------

    def _renforge_h_ping(payload):
        return {"ok": True, "pong": True}

    def _renforge_h_get_metrics_handler(payload):
        return {"ok": True, "metrics": _renforge_h_get_metrics(payload)}

    def _renforge_h_get_audio_state_handler(payload):
        return {"ok": True, "channels": _renforge_h_get_audio_state(payload)}

    def _renforge_eval_refused():
        """eval and set_var run arbitrary code; a launch can switch them off.

        Fails closed: only an unset RENFORGE_BRIDGE_EVAL or "on" (any case)
        leaves them on, so "OFF", "0" or "false" switch them off too.
        """
        setting = os.environ.get("RENFORGE_BRIDGE_EVAL")
        if setting is not None and setting.strip().casefold() != "on":
            return {"ok": False, "error": "eval_disabled",
                    "message": "This session's bridge does not run code (no debug or editor toolset)."}
        return None

    def _renforge_h_eval(payload):
        refused = _renforge_eval_refused()
        if refused:
            return refused
        expr = (payload or {}).get("expr", "")
        value = eval(expr, {"__builtins__": __builtins__}, vars(renpy.store))
        return {"expr": expr, "value": _renforge_jsonable(value)}

    def _renforge_h_get_var(payload):
        name = (payload or {}).get("name")
        return {"name": name, "value": _renforge_jsonable(getattr(renpy.store, name))}

    def _renforge_h_set_var(payload):
        refused = _renforge_eval_refused()
        if refused:
            return refused
        name = (payload or {}).get("name")
        value = (payload or {}).get("value")
        setattr(renpy.store, name, value)
        return {"name": name, "value": value, "ok": True}

    def _renforge_h_screenshot(payload):
        payload = payload or {}
        width = int(payload.get("width", 0) or 0)
        height = int(payload.get("height", 0) or 0)
        # A single dimension keeps the game's aspect ratio.
        note = None
        logical_width = getattr(renpy.config, "screen_width", None)
        logical_height = getattr(renpy.config, "screen_height", None)
        if logical_width and logical_height:
            if width and not height:
                height = max(1, int(round(width * logical_height / float(logical_width))))
            elif height and not width:
                width = max(1, int(round(height * logical_width / float(logical_height))))
        elif (width and not height) or (height and not width):
            # Never silently ignore the requested size: without the logical
            # ratio the frame comes back at native resolution, so say so.
            width = height = 0
            note = "aspect ratio unavailable; captured at native resolution"
        size = (width, height) if (width and height) else None
        data = renpy.screenshot_to_bytes(size)  # PNG bytes
        reply = {
            "format": "png",
            "base64": base64.b64encode(data).decode("ascii"),
            # The digest lets an external client use the exact frame it
            # inspected as an optimistic click guard, without storing image
            # data in the bridge process.
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if note:
            reply["note"] = note
        return reply

    def _renforge_h_advance(payload):
        # Post a "dismiss" event (the keymap action that advances dialogue).
        # queue_event is documented as thread-safe; the interaction loop
        # consumes it on the next frame.
        renpy.exports.queue_event("dismiss")
        return {"ok": True}

    # Readable input names that are semantic Ren'Py keymap actions. Keeping
    # these as names (rather than SDK integer constants) means they continue
    # to respect a game's customized config.keymap.
    _RENFORGE_INPUT_KEYMAP = {
        "enter": ("input_enter", "dismiss", "button_select"),
        "return": ("input_enter", "dismiss", "button_select"),
        "esc": ("game_menu",),
        "escape": ("game_menu",),
        "up": ("focus_up", "input_up", "viewport_uparrow", "bar_up"),
        "down": ("focus_down", "input_down", "viewport_downarrow", "bar_down"),
        "left": ("focus_left", "input_left", "viewport_leftarrow", "bar_left"),
        "right": ("focus_right", "input_right", "viewport_rightarrow", "bar_right"),
        "pageup": ("rollback", "viewport_pageup"),
        "pagedown": ("rollforward", "viewport_pagedown"),
        "backspace": ("input_backspace",),
        "delete": ("input_delete", "save_delete"),
        "home": ("input_home",),
        "end": ("input_end",),
        "space": ("dismiss", "button_select"),
        "tab": ("toggle_skip",),
    }

    # A small explicit set of keys without a useful Ren'Py semantic action.
    # These are posted as real KEYDOWN/KEYUP pairs so custom screens can bind
    # them with a normal key statement.
    _RENFORGE_DIRECT_KEY_ATTRS = {
        "f1": "K_F1",
        "f2": "K_F2",
        "f3": "K_F3",
        "f4": "K_F4",
        "f5": "K_F5",
        "f6": "K_F6",
        "f7": "K_F7",
        "f8": "K_F8",
        "f9": "K_F9",
        "f10": "K_F10",
        "f11": "K_F11",
        "f12": "K_F12",
    }

    def _renforge_act_key_attrs():
        # Physical keys for renforge_act. Semantic keymap names are the wrong
        # events here: "left" would move focus, and "space" would dismiss.
        attrs = {
            "up": "K_UP",
            "down": "K_DOWN",
            "left": "K_LEFT",
            "right": "K_RIGHT",
            "escape": "K_ESCAPE",
            "esc": "K_ESCAPE",
            "return": "K_RETURN",
            "enter": "K_RETURN",
            "space": "K_SPACE",
            "tab": "K_TAB",
        }
        for letter in _RENFORGE_ACT_KEY_LETTERS:
            attrs[letter] = "K_" + letter
        return attrs

    _RENFORGE_ACT_KEY_LETTERS = "abcdefghijklmnopqrstuvwxyz"
    _RENFORGE_ACT_KEY_ATTRS = _renforge_act_key_attrs()

    def _renforge_act_key_list():
        """The act keys an error lists, built from the key table so they never drift.

        Letters collapse to "a-z"; an alias of a listed key ("esc", "enter")
        is left out.
        """
        names = ["a-z"]
        listed = set()
        for name, attr in _RENFORGE_ACT_KEY_ATTRS.items():
            if (len(name) == 1 and name in _RENFORGE_ACT_KEY_LETTERS) or attr in listed:
                continue
            listed.add(attr)
            names.append(name)
        return ", ".join(names)

    _RENFORGE_ACT_KEY_LIST = _renforge_act_key_list()

    def _renforge_focused_input():
        """Return the focused Ren'Py Input, or an explicit diagnostic."""
        display = getattr(renpy, "display", None)
        focus = getattr(display, "focus", None)
        get_focused = getattr(focus, "get_focused", None)
        if not callable(get_focused):
            return None, "cannot verify focused Ren'Py Input (focus API unavailable)"
        try:
            widget = get_focused()
        except Exception as exc:
            return None, "cannot verify focused Ren'Py Input: %s" % exc

        behavior = getattr(display, "behavior", None)
        input_type = getattr(behavior, "Input", None)

        def _is_input(candidate):
            if candidate is None:
                return False
            if callable(input_type):
                try:
                    if isinstance(candidate, input_type):
                        return True
                except TypeError:
                    pass
            return getattr(getattr(candidate, "__class__", None), "__name__", "") == "Input"

        # Ren'Py can have an active Input screen without assigning keyboard
        # focus yet (notably after a warp under Xvfb). Select the visible Input
        # through the engine focus API before posting TEXTINPUT events.
        if widget is None:
            change_focus = getattr(focus, "change_focus", None)
            for candidate in list(getattr(focus, "focus_list", None) or []):
                candidate_widget = getattr(candidate, "widget", None)
                if not _is_input(candidate_widget) or not callable(change_focus):
                    continue
                try:
                    change_focus(candidate)
                    widget = get_focused()
                except Exception:
                    widget = None
                if _is_input(widget):
                    break

        if widget is None:
            get_screen = getattr(renpy, "get_screen", None)
            force_focus = getattr(focus, "force_focus", None)
            if callable(get_screen) and callable(force_focus):
                try:
                    input_screen = get_screen("input")
                    input_widget = getattr(input_screen, "widgets", {}).get("input")
                    if _is_input(input_widget):
                        force_focus(input_widget)
                        widget = get_focused()
                except Exception:
                    widget = None

        if widget is None:
            return None, "no focused Ren'Py Input; text was not sent"
        if _is_input(widget):
            return widget, None
        return None, "no focused Ren'Py Input; focused widget is %s" % (
            getattr(getattr(widget, "__class__", None), "__name__", "unknown"),
        )

    def _renforge_input_char_allowed(widget, character):
        """Whether the Input's ``allow`` and ``exclude`` (strings or regexes) let ``character`` in."""
        allow = getattr(widget, "allow", None)
        exclude = getattr(widget, "exclude", None)
        if allow:
            search = getattr(allow, "search", None)
            if callable(search):
                if search(character) is None:
                    return False
            elif character not in allow:
                return False
        if exclude:
            search = getattr(exclude, "search", None)
            if callable(search):
                if search(character) is not None:
                    return False
            elif character in exclude:
                return False
        return True

    def _renforge_input_filtered(widget, text):
        """``text`` as the Input would keep it when typed: allow, exclude, length."""
        # Input.event applies the limit with `if self.length:`, so 0 and None
        # both mean no limit.
        length = getattr(widget, "length", None)
        if isinstance(length, bool) or not isinstance(length, builtins.int) or length <= 0:
            length = None
        accepted = ""
        for character in text:
            if not _renforge_input_char_allowed(widget, character):
                continue
            accepted += character
            if length is not None and len(accepted) >= length:
                break
        return accepted

    def _renforge_replace_input_text(widget, text):
        """Put `text` in the field, dropping whatever was already there.

        Returns the stored string, False when the field rejects it, or None
        when this widget has no Ren'Py input value to replace. An error from
        the game's own callback (the InputValue's ``set_text``, which
        ``update_text`` runs) propagates, so the reply names it.
        """
        filtered = _renforge_input_filtered(widget, text)
        if text and not filtered:
            return False
        update = getattr(widget, "update_text", None)
        if callable(update):
            try:
                widget.caret_pos = len(filtered)
            except Exception:
                pass
            try:
                update(filtered, True, True)
            except TypeError as exc:
                # Retry without check_size only when the call itself was
                # refused: that TypeError has no frame inside update_text.
                # Ren'Py's Input takes check_size on every supported 8.x.
                traceback = exc.__traceback__
                if traceback is None or traceback.tb_next is not None:
                    raise
                update(filtered, True)
            current = getattr(widget, "content", None)
            if current != filtered:
                return False
            return filtered
        value = getattr(widget, "value", None)
        set_text = getattr(value, "set_text", None) if value is not None else None
        if not callable(set_text):
            return None
        set_text(filtered)
        try:
            widget.content = filtered
        except Exception:
            pass
        try:
            widget.caret_pos = len(filtered)
        except Exception:
            pass
        return filtered

    def _renforge_post_key(keycode, hold=None, text=""):
        """Post a physical key: down and up, or only down (``hold`` True) or up (False).

        Returns the phases posted, as "keydown"/"keyup".
        """
        # A keyboard's key-down carries the character it types, and Ren'Py
        # matches a bare `key "d"` binding on that character.
        phases = (pygame.KEYDOWN, pygame.KEYUP) if hold is None else ((pygame.KEYDOWN,) if hold else (pygame.KEYUP,))
        for phase in phases:
            pygame.event.post(pygame.event.Event(phase, {
                "key": keycode, "mod": getattr(pygame, "KMOD_NONE", 0),
                "unicode": text if phase == pygame.KEYDOWN else "", "repeat": 0,
            }))
        return ["keydown" if phase == pygame.KEYDOWN else "keyup" for phase in phases]


    def _renforge_type_input(widget, text, submit):
        """Shared replacement/filtering and submission for both input tools."""
        replaced = _renforge_replace_input_text(widget, text)
        if replaced is False:
            return {"ok": False, "error": "text_rejected"}
        if replaced is None:
            for character in text:
                pygame.event.post(pygame.event.Event(pygame.TEXTINPUT, {"text": character}))
            stored = text
        else:
            stored = replaced
        if submit:
            renpy.exports.queue_event("input_enter")
        result = {"ok": True, "characters": len(stored), "submitted": submit}
        if replaced is not None:
            result["replaced"] = True
        return result


    def _renforge_h_send_input(payload):
        payload = payload or {}
        supplied = [
            name
            for name in ("text", "key", "scroll", "drag")
            if name in payload and payload.get(name) is not None
        ]
        if len(supplied) != 1:
            return {
                "ok": False,
                "error": "exactly one of text, key, or scroll is required",
            }

        submit = payload.get("submit", False)
        if not isinstance(submit, bool):
            return {"ok": False, "error": "submit must be a boolean"}
        if supplied[0] != "text" and submit:
            return {"ok": False, "error": "submit is only valid with text input"}

        if supplied[0] == "text":
            text = payload.get("text")
            if not isinstance(text, str):
                return {"ok": False, "error": "text must be a string"}
            if pygame is None:
                return {"ok": False, "error": "pygame_sdl2 event API is unavailable"}
            focused, focus_error = _renforge_focused_input()
            if focus_error is not None:
                return {"ok": False, "error": focus_error}
            result = _renforge_type_input(focused, text, submit)
            result["mode"] = "text"
            return result

        if supplied[0] == "key":
            key = payload.get("key")
            if not isinstance(key, str) or not key.strip():
                return {"ok": False, "error": "key must be a non-empty string"}
            key = key.strip().casefold()
            semantic = _RENFORGE_INPUT_KEYMAP.get(key)
            if semantic is not None:
                renpy.exports.queue_event(list(semantic))
                return {"ok": True, "mode": "key", "key": key, "event": semantic[0]}

            attr_name = _RENFORGE_DIRECT_KEY_ATTRS.get(key)
            keycode = (
                getattr(pygame, attr_name, None)
                if pygame is not None and attr_name is not None
                else None
            )
            if keycode is None:
                supported = sorted(set(_RENFORGE_INPUT_KEYMAP) | set(_RENFORGE_DIRECT_KEY_ATTRS))
                return {
                    "ok": False,
                    "error": "unknown key %r; supported keys: %s" % (key, ", ".join(supported)),
                }
            if pygame is None:
                return {"ok": False, "error": "pygame_sdl2 event API is unavailable"}
            _renforge_post_key(keycode)
            return {"ok": True, "mode": "key", "key": key, "keycode": keycode}


        if supplied[0] == "drag":
            drag = payload.get("drag")
            if not isinstance(drag, builtins.dict):
                return {"ok": False, "error": "drag must be an object with points and button"}
            raw_points = drag.get("points")
            if not isinstance(raw_points, builtins.list) or not raw_points:
                return {"ok": False, "error": "drag points must be a non-empty list"}
            coordinate_space = str(drag.get("coordinate_space", "logical") or "logical").casefold()
            if coordinate_space not in ("logical", "screenshot"):
                return {"ok": False, "error": "coordinate_space must be logical or screenshot"}
            button = drag.get("button", 1)
            if isinstance(button, bool) or not isinstance(button, builtins.int):
                return {"ok": False, "error": "drag button must be an integer"}
            frame_data = None
            if coordinate_space == "screenshot":
                frame_data = renpy.screenshot_to_bytes(None)
            logical_points = []
            for raw_point in raw_points:
                if not isinstance(raw_point, builtins.list) or len(raw_point) != 2:
                    return {"ok": False, "error": "drag points must be coordinate pairs"}
                raw_x, raw_y = raw_point
                if (
                    isinstance(raw_x, bool)
                    or not isinstance(raw_x, (builtins.int, builtins.float))
                    or isinstance(raw_y, bool)
                    or not isinstance(raw_y, (builtins.int, builtins.float))
                ):
                    return {"ok": False, "error": "drag points must be numeric"}
                try:
                    point_x = int(round(float(raw_x)))
                    point_y = int(round(float(raw_y)))
                except (TypeError, ValueError, OverflowError):
                    return {"ok": False, "error": "drag points must be numeric"}
                if point_x < 0 or point_y < 0:
                    return {"ok": False, "error": "drag coordinates must be non-negative"}
                point_x, point_y, frame_data, coordinate_error = _renforge_to_logical_coordinates(
                    point_x, point_y, coordinate_space, frame_data
                )
                if coordinate_error is not None:
                    return {"ok": False, "error": coordinate_error}
                logical_points.append([point_x, point_y])
            if pygame is None:
                return {"ok": False, "error": "pygame_sdl2 event API is unavailable"}
            interface = getattr(getattr(renpy, "display", None), "interface", None)
            if interface is not None:
                try:
                    interface.mouse_focused = True
                except Exception:
                    pass
                try:
                    interface.ignore_touch = False
                except Exception:
                    pass
            event_factory = getattr(getattr(pygame, "event", None), "Event", None)
            post = getattr(getattr(pygame, "event", None), "post", None)
            down_type = getattr(pygame, "MOUSEBUTTONDOWN", None)
            motion_type = getattr(pygame, "MOUSEMOTION", None)
            up_type = getattr(pygame, "MOUSEBUTTONUP", None)
            if (
                not callable(event_factory)
                or not callable(post)
                or down_type is None
                or motion_type is None
                or up_type is None
            ):
                return {"ok": False, "error": "pygame_sdl2 event API is unavailable"}

            def make_event(event_type, attrs):
                try:
                    return event_factory(event_type, **attrs)
                except TypeError:
                    return event_factory(event_type, attrs)

            first_x, first_y = logical_points[0]
            button_payload = {
                "button": button,
                "pos": (first_x, first_y),
                "x": first_x,
                "y": first_y,
                "touch": False,
                "test": True,
                "mod": 0,
            }
            post(make_event(down_type, button_payload))
            previous_x, previous_y = first_x, first_y
            held_buttons = tuple(int(button == candidate) for candidate in (1, 2, 3))
            for point_x, point_y in logical_points[1:]:
                post(
                    make_event(
                        motion_type,
                        {
                            "pos": (point_x, point_y),
                            "rel": (point_x - previous_x, point_y - previous_y),
                            "buttons": held_buttons,
                        },
                    )
                )
                previous_x, previous_y = point_x, point_y
            last_x, last_y = logical_points[-1]
            button_payload = {
                "button": button,
                "pos": (last_x, last_y),
                "x": last_x,
                "y": last_y,
                "touch": False,
                "test": True,
                "mod": 0,
            }
            post(make_event(up_type, button_payload))
            return {
                "ok": True,
                "mode": "drag",
                "points": logical_points,
                "button": button,
            }

        scroll = payload.get("scroll")
        if not isinstance(scroll, builtins.dict):
            return {"ok": False, "error": "scroll must be an object with x, y, and direction"}
        try:
            raw_x, raw_y = scroll.get("x"), scroll.get("y")
            if isinstance(raw_x, bool) or isinstance(raw_y, bool):
                raise ValueError
            x, y = int(round(float(raw_x))), int(round(float(raw_y)))
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "scroll requires numeric x and y"}
        if x < 0 or y < 0:
            return {"ok": False, "error": "scroll coordinates must be non-negative"}

        direction = scroll.get("direction")
        if direction is not None:
            direction = str(direction).casefold()
            direction = {"wheelup": "up", "wheeldown": "down"}.get(direction, direction)
            if direction not in ("up", "down"):
                return {"ok": False, "error": "scroll direction must be up or down"}

        amount = scroll.get("amount", scroll.get("delta", 1))
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return {"ok": False, "error": "scroll amount must be a non-zero integer"}
        if isinstance(amount, float) and not amount.is_integer():
            return {"ok": False, "error": "scroll amount must be a non-zero integer"}
        amount = int(amount)
        if amount == 0:
            return {"ok": False, "error": "scroll amount must be a non-zero integer"}
        if direction is None:
            direction = "up" if amount < 0 else "down"
        amount = abs(amount)

        coordinate_space = str(scroll.get("coordinate_space", "logical") or "logical").casefold()
        if coordinate_space not in ("logical", "screenshot"):
            return {"ok": False, "error": "coordinate_space must be logical or screenshot"}
        frame_data = None
        if coordinate_space == "screenshot":
            frame_data = renpy.screenshot_to_bytes(None)
        x, y, frame_data, coordinate_error = _renforge_to_logical_coordinates(
            x, y, coordinate_space, frame_data
        )
        if coordinate_error is not None:
            return {"ok": False, "error": coordinate_error}
        if pygame is None:
            return {"ok": False, "error": "pygame_sdl2 event API is unavailable"}
        interface = getattr(getattr(renpy, "display", None), "interface", None)
        if interface is not None:
            try:
                interface.mouse_focused = True
            except Exception:
                pass
            try:
                interface.ignore_touch = False
            except Exception:
                pass
        button = 4 if direction == "up" else 5
        for _ in range(amount):
            event = pygame.event.Event(
                pygame.MOUSEBUTTONDOWN,
                {"button": button, "pos": (x, y), "x": x, "y": y},
            )
            pygame.event.post(event)
        return {
            "ok": True,
            "mode": "scroll",
            "x": x,
            "y": y,
            "direction": direction,
            "amount": amount,
        }

    def _renforge_invoke(fn):
        # Schedule work for the interaction loop. Prefer invoke_in_main_thread
        # when already on the main thread would re-enter drain; callers that
        # already run inside renforge_drain_bridge should call fn() directly
        # unless the work must raise engine control-flow exceptions (load/quit).
        invoke = getattr(renpy, "invoke_in_main_thread", None)
        if callable(invoke):
            invoke(fn)
        else:
            fn()

    def _renforge_run_action(action):
        run = getattr(renpy, "run", None)
        if not callable(run):
            run = getattr(getattr(renpy, "exports", None), "run", None)
        if not callable(run):
            raise RuntimeError("renpy.run is unavailable")
        return run(action)

    def _renforge_history_index():
        """Best-effort length of the rollback log."""
        game = getattr(renpy, "game", None)
        log = getattr(game, "log", None)
        entries = getattr(log, "log", None)
        if entries is None:
            return None
        try:
            return len(entries)
        except Exception:
            return None

    def _renforge_newest_quick_slot():
        newest = getattr(renpy, "newest_slot", None)
        if callable(newest):
            try:
                slot = newest("quick")
                if slot:
                    return str(slot)
            except Exception:
                pass
        # Fallbacks used across Ren'Py versions / templates.
        for candidate in ("quick-1", "quick-2", "quick-3", "_reload-1"):
            can_load = getattr(renpy, "can_load", None)
            if callable(can_load):
                try:
                    if can_load(candidate):
                        return candidate
                except Exception:
                    continue
        return "quick-1"

    def _renforge_emit_business(event_name, **data):
        bridge = _renforge_runtime.bridge
        if bridge is None:
            return None
        payload = {"event": event_name}
        payload.update(data)
        # type matches the business event name for easy filtering; event is kept
        # for doc-compatible clients that look for the nested field.
        return bridge.push_event(event_name, payload)

    def _renforge_correlation_from_payload(payload):
        payload = payload or {}
        for key in ("interaction_id", "correlation_id"):
            value = payload.get(key)
            if value is None or value == "":
                continue
            try:
                return str(value)
            except Exception:
                continue
        return None

    def _renforge_h_control(payload):
        payload = payload or {}
        action = str(payload.get("action", ""))
        # Names that exist on config.keymap / the default Keymap underlay.
        # Note: there is no "toggle_auto" or "quick_save"/"quick_load" keymap
        # entry — those used to return ok while doing nothing.
        key_events = {
            "advance": "dismiss",
            "rollback": "rollback",
            "toggle_skip": "toggle_skip",
            "toggle_auto": "toggle_afm",
            "toggle_afm": "toggle_afm",
            "game_menu": "game_menu",
            "hide_windows": "hide_windows",
        }
        if action in key_events:
            event_name = key_events[action]
            bridge = _renforge_runtime.bridge
            before_history = _renforge_history_index()
            before_skip = getattr(renpy.config, "skipping", None)
            before_afm = None
            try:
                prefs = getattr(renpy.store, "_preferences", None)
                before_afm = bool(getattr(prefs, "afm_enable", False)) if prefs is not None else None
            except Exception:
                before_afm = None
            renpy.exports.queue_event(event_name)
            result = {"ok": True, "action": action, "event": event_name}
            if action == "rollback":
                after_history = before_history
                if before_history is not None:
                    after_history = max(0, before_history - 1)
                business = _renforge_emit_business(
                    "rollback.completed",
                    from_history_index=before_history,
                    to_history_index=after_history,
                )
                if business is not None:
                    result["effect"] = {
                        "event": "rollback.completed",
                        "from_history_index": before_history,
                        "to_history_index": after_history,
                    }
            elif action == "toggle_skip":
                if bridge is not None:
                    # Hint for the watcher: next skip transition was agent-driven.
                    bridge._skip_reason_hint = "user_click"
                business = _renforge_emit_business(
                    "skip.changed",
                    previous=before_skip,
                    requested=True,
                )
                if business is not None:
                    result["effect"] = {"event": "skip.changed", "previous": before_skip}
            elif action in ("toggle_auto", "toggle_afm"):
                business = _renforge_emit_business(
                    "auto.changed",
                    previous=before_afm,
                    requested=True,
                )
                if business is not None:
                    result["effect"] = {"event": "auto.changed", "previous": before_afm}
            return result
        if action == "quick_save":
            quick_save = getattr(renpy.store, "QuickSave", None)
            if not callable(quick_save):
                _renforge_emit_business("quick_save.failed", reason="unavailable")
                return {"ok": False, "error": "QuickSave is unavailable", "action": action}
            try:
                _renforge_run_action(quick_save())
            except Exception as exc:
                _renforge_emit_business("quick_save.failed", reason=str(exc))
                return {"ok": False, "error": "quick_save failed: %s" % exc, "action": action}
            slot = _renforge_newest_quick_slot()
            path = None
            try:
                savedir = getattr(renpy.config, "savedir", None)
                if savedir and slot:
                    path = os.path.join(str(savedir), "%s.save" % slot)
            except Exception:
                path = None
            business = _renforge_emit_business(
                "quick_save.completed",
                slot=slot,
                path=path,
            )
            result = {"ok": True, "action": action, "slot": slot}
            if path:
                result["path"] = path
            if business is not None:
                result["effect"] = {
                    "event": "quick_save.completed",
                    "slot": slot,
                    "path": path,
                }
            return result
        if action == "quick_load":
            quick_load = getattr(renpy.store, "QuickLoad", None)
            if not callable(quick_load):
                _renforge_emit_business("quick_load.failed", reason="unavailable")
                return {"ok": False, "error": "QuickLoad is unavailable", "action": action}
            # Load raises FullRestartException; schedule it so the interaction
            # loop can propagate engine control-flow instead of catching it here.
            load_action = quick_load(confirm=False)
            slot = _renforge_newest_quick_slot()
            bridge = _renforge_runtime.bridge
            restored_label = bridge.current_label if bridge is not None else None
            restored_dialogue = bridge.last_say if bridge is not None else None

            def _do_quick_load():
                _renforge_run_action(load_action)

            _renforge_invoke(_do_quick_load)
            business = _renforge_emit_business(
                "quick_load.completed",
                slot=slot,
                restored_label=restored_label,
                restored_dialogue=restored_dialogue,
            )
            result = {
                "ok": True,
                "action": action,
                "slot": slot,
                "restored_label": restored_label,
            }
            if business is not None:
                result["effect"] = {
                    "event": "quick_load.completed",
                    "slot": slot,
                    "restored_label": restored_label,
                }
            return result
        # A command that ends the game or reloads it cancels any progression in
        # flight. The agent never sees "cancelled" for a quit: the act that was
        # progressing reports the process exit (exited) instead.
        if action == "reload_script":
            _renforge_progress_finish("cancelled")
            _renforge_invoke(renpy.reload_script)
            return {"ok": True, "action": action}
        if action == "restart_interaction":
            _renforge_invoke(renpy.restart_interaction)
            return {"ok": True, "action": action}
        if action == "quit":
            _renforge_progress_finish("cancelled")
            _renforge_invoke(renpy.quit)
            return {"ok": True, "action": action}
        return {"ok": False, "error": "unknown control action: %s" % action}

    def _renforge_slot_name_error(slot):
        """Why ``slot`` cannot name a save slot, as a reply, or None.

        Ren'Py builds a save file's path as ``os.path.join(savedir, slot +
        suffix)``, so a slot that is a path (absolute, with a drive, with
        either separator or NUL, or ``.``/``..``) would read or write outside
        the save folder, the player's own saves included. The host refuses
        these first (``save_isolation.slot_name_error``); this is the second
        line for a caller that reaches the bridge directly. A colon is refused
        too: on Windows ``page:name`` is stream syntax, writing a stream of
        the file ``page`` rather than a save file.
        """
        import ntpath
        if (any(char in slot for char in ("/", "\\", "\x00", ":")) or slot.strip() in (".", "..")
                or ntpath.splitdrive(slot)[0]):
            return {"ok": False, "code": "SAVE_SLOT_INVALID",
                "error": "slot must be a save slot name such as 1-1 or quick-1, not a path"}
        return None

    def _renforge_h_save_slot(payload):
        payload = payload or {}
        slot = payload.get("slot")
        extra_info = payload.get("extra_info", "")
        if not isinstance(slot, str) or not slot.strip():
            return {"ok": False, "error": "save slot is required"}
        slot_error = _renforge_slot_name_error(slot)
        if slot_error is not None:
            return slot_error
        if extra_info is None:
            extra_info = ""
        if not isinstance(extra_info, str):
            return {"ok": False, "error": "extra_info must be a string"}

        can_save = getattr(renpy, "can_save", None)
        if callable(can_save):
            try:
                allowed = bool(can_save())
            except Exception as exc:
                return {
                    "ok": False,
                    "error": "cannot determine whether saving is available: %s" % exc,
                }
        else:
            config = getattr(renpy, "config", None)
            store = getattr(renpy, "store", None)
            allowed = bool(config and getattr(config, "save", True))
            allowed = allowed and not bool(store and getattr(store, "main_menu", False))
            allowed = allowed and not bool(store and getattr(store, "_in_replay", False))

        if not allowed:
            return {"ok": False, "error": "saving is unavailable in the current game state"}

        try:
            renpy.save(slot, extra_info=extra_info)
        except Exception as exc:
            _renforge_emit_business("save.failed", slot=slot, reason=str(exc))
            return {"ok": False, "error": "save failed: %s" % exc}

        _renforge_emit_business("save.completed", slot=slot, extra_info=extra_info)
        result = {"ok": True, "slot": slot, "extra_info": extra_info}
        savedir = getattr(getattr(renpy, "config", None), "savedir", None)
        if savedir:
            result["savedir"] = str(savedir)
        return result

    def _renforge_h_load_slot(payload):
        payload = payload or {}
        slot = payload.get("slot")
        if not isinstance(slot, str) or not slot.strip():
            return {"ok": False, "error": "save slot is required"}
        slot_error = _renforge_slot_name_error(slot)
        if slot_error is not None:
            return slot_error

        rescan_error = _renforge_rescan_saves()
        can_load = getattr(renpy, "can_load", None)
        if callable(can_load):
            try:
                exists = bool(can_load(slot))
            except Exception as exc:
                return {"ok": False, "error": "cannot inspect save slot: %s" % exc}
        else:
            list_slots = getattr(renpy, "list_slots", None)
            if not callable(list_slots):
                return {"ok": False, "error": "save slot lookup is unavailable"}
            try:
                exists = slot in list_slots()
            except Exception as exc:
                return {"ok": False, "error": "cannot inspect save slot: %s" % exc}

        if not exists:
            result = {"ok": False, "error": "save slot not found: %s" % slot}
            if rescan_error is not None:
                # The slot list may be stale, so say why it could be missing.
                result["error"] += " (save rescan failed: %s)" % rescan_error
                result["rescan_error"] = rescan_error
            return result

        load = getattr(renpy, "load", None)
        if not callable(load):
            return {"ok": False, "error": "save loading is unavailable"}

        # From 8.2 invoke_in_main_thread queues the load and returns, so this
        # handler reports it below. Only an inline load (no queue) ends this
        # request with the engine's restart and must report it here.
        inline = not callable(getattr(renpy, "invoke_in_main_thread", None))

        def _do_load():
            try:
                load(slot)
            except Exception as exc:
                if inline and _renforge_is_script_control_flow(exc):
                    # The engine restarts into the loaded game after the reply.
                    _renforge_emit_business("load.completed", slot=slot, restored_label=None)
                    restart = {"ok": True, "slot": slot, "restored_label": None, "restarting": True}
                    if rescan_error is not None:
                        restart["rescan_error"] = rescan_error
                    setattr(exc, "renforge_result", restart)
                raise

        _renforge_invoke(_do_load)
        bridge = _renforge_runtime.bridge
        restored_label = bridge.current_label if bridge is not None else None
        _renforge_emit_business(
            "load.completed",
            slot=slot,
            restored_label=restored_label,
        )
        result = {"ok": True, "slot": slot, "restored_label": restored_label}
        if rescan_error is not None:
            result["rescan_error"] = rescan_error
        return result

    def _renforge_rescan_saves():
        """See save files copied in since Ren'Py's own scan, which runs every few seconds.

        Best effort: a failure leaves Ren'Py's previous slot list in place, so
        it is returned (as text) for the caller to report, never raised.
        """
        location = getattr(getattr(renpy, "loadsave", None), "location", None)
        scan = getattr(location, "scan", None)
        if not callable(scan):
            return None
        try:
            scan()
        except Exception as exc:
            return "%s: %s" % (type(exc).__name__, exc)
        return None

    def _renforge_h_list_slots(payload):
        payload = payload or {}
        regexp = payload.get("regexp")
        if regexp is not None and not isinstance(regexp, str):
            return {"ok": False, "error": "regexp must be a string"}

        list_slots = getattr(renpy, "list_slots", None)
        if not callable(list_slots):
            return {"ok": False, "error": "save slot listing is unavailable"}
        rescan_error = _renforge_rescan_saves()
        try:
            slot_names = list_slots(regexp=regexp)
        except Exception as exc:
            return {"ok": False, "error": "could not list save slots: %s" % exc}

        slot_json = getattr(renpy, "slot_json", None)
        slot_mtime = getattr(renpy, "slot_mtime", None)
        slots = []
        for name in slot_names:
            try:
                metadata = slot_json(name) if callable(slot_json) else None
            except Exception:
                metadata = None
            extra_info = ""
            if isinstance(metadata, builtins.dict):
                extra_info = metadata.get("_save_name", "")
            try:
                mtime = slot_mtime(name) if callable(slot_mtime) else None
            except Exception:
                mtime = None
            slots.append(
                {
                    "name": str(name),
                    "extra_info": _renforge_jsonable(extra_info),
                    "mtime": _renforge_jsonable(mtime),
                }
            )

        # Names the player's own save folder, which import reads from even
        # when the game ships only compiled scripts.
        save_directory = getattr(renpy.config, "save_directory", None)
        result = {"ok": True, "slots": slots, "save_directory": _renforge_jsonable(save_directory)}
        if rescan_error is not None:
            result["rescan_error"] = rescan_error
        return result

    def _renforge_h_poll_events(payload):
        payload = payload or {}
        since = int(payload.get("since", 0) or 0)
        bridge = _renforge_runtime.bridge
        if bridge is None:
            return {"events": [], "cursor": 0}
        events = [e for e in list(bridge.events) if e["seq"] > since]
        cursor = bridge.event_seq
        oldest = bridge.events[0]["seq"] if bridge.events else cursor + 1
        return {"events": events, "cursor": cursor, "oldest_cursor": oldest,
                "truncated": since < oldest - 1}

    def _renforge_screen_name(focus):
        scr = getattr(focus, "screen", None)
        name = getattr(scr, "screen_name", None)
        if name is None:
            name = getattr(focus, "screen_name", None)
        if not name:
            return None
        try:
            return name[0] if isinstance(name, (list, tuple)) else str(name)
        except Exception:
            return None

    def _renforge_transform_hides(node):
        """A transform that currently draws nothing (alpha or zoom at 0)."""
        if not isinstance(node, getattr(getattr(renpy.display, "transform", None), "Transform", ())):
            return False
        for name in ("alpha", "zoom", "xzoom", "yzoom"):
            try:
                value = getattr(node, name)
            except Exception:
                continue
            if isinstance(value, (builtins.int, builtins.float)) and not isinstance(value, bool) and value == 0:
                return True
        return False


    def _renforge_child_text(widget):
        """Visible label: the Text displayables inside a control, in order."""
        parts = []
        stack = [widget]
        seen = set()
        while stack and len(seen) < 64:
            node = stack.pop()
            if node is None or id(node) in seen:
                continue
            seen.add(id(node))
            # Like Ren'Py's self-voicing, nested controls keep their own text.
            if node is not widget and getattr(node, "focusable", None):
                continue
            # Text the game keeps hidden (a hover card at alpha 0) is not
            # part of the label.
            if _renforge_transform_hides(node):
                continue
            if node.__class__.__name__ == "Text":
                text = _renforge_observe_text_string(node)
                if text:
                    parts.append(text)
                continue
            visit = getattr(node, "visit", None)
            try:
                children = visit() if callable(visit) else ()
            except Exception:
                children = ()
            stack.extend(reversed([child for child in children or () if child is not None]))
        return " ".join(parts)

    def _renforge_tts_marks_selected(widget):
        """Whether ``Button._tts_all`` added " selected" to this widget's label.

        Ren'Py 8.5+ adds it when the style prefix is ``selected_``; older
        engines when ``is_selected()``. From 8.2 only when the alt is the
        default hover alt. Any other " selected" belongs to the alt text.
        """
        style = getattr(widget, "style", None)
        hover_alt = getattr(style, "_hover_alt", None)
        if callable(hover_alt):
            try:
                if getattr(style, "alt", None) != hover_alt():
                    return False
            except Exception:
                pass
        prefix = getattr(style, "prefix", None)
        if isinstance(prefix, builtins.str):
            return prefix.startswith("selected_")
        is_selected = getattr(widget, "is_selected", None)
        if callable(is_selected):
            try:
                return bool(is_selected())
            except Exception:
                return False
        return False

    def _renforge_focus_text(widget):
        """Best-effort label for a Ren'Py focus widget.

        Prefer the control's own Text children. Self-voicing text
        (``_tts_all``) appends state words such as "selected" and "bar", so it
        is only the fallback, with those words removed. Ren'Py 8.5 requires
        ``_tts_all(raw)``; older engines take no argument.
        """
        if widget is None:
            return ""
        if widget.__class__.__name__ != "Text":
            text = _renforge_child_text(widget)
            if text:
                return text
        text = None
        for method_name in ("_tts_all", "get_text"):
            method = getattr(widget, method_name, None)
            if not callable(method):
                continue
            try:
                if method_name == "_tts_all":
                    try:
                        text = method(False)
                    except TypeError:
                        text = method()
                else:
                    text = method()
            except Exception:
                continue
            if text:
                break
        if isinstance(text, builtins.str):
            translate = getattr(getattr(renpy, "minstore", None), "__", None)

            def suffixes(word):
                words = {word}
                try:
                    words.add(str(translate(word)) if callable(translate) else word)
                except Exception:
                    pass
                return words

            if widget.__class__.__name__ == "Bar":
                for word in suffixes("bar"):
                    if word and text.endswith(word):
                        text = text[:-len(word)]
                        break
            elif _renforge_tts_marks_selected(widget):
                for word in suffixes("selected"):
                    if word and text.endswith(" " + word):
                        text = text[:-len(word) - 1]
                        break
        if text is None:
            for attr_name in ("text", "label", "caption", "value"):
                value = getattr(widget, attr_name, None)
                if value is not None and not callable(value):
                    text = value
                    if text:
                        break
        if isinstance(text, (list, tuple)):
            text = " ".join(str(part) for part in text if part is not None)
        if text is None:
            return ""
        try:
            return str(text).strip()
        except Exception:
            return ""

    def _renforge_focus_type(focus, widget):
        # Some Ren'Py displayables expose a semantic type; otherwise use the
        # displayable class name and normalize common controls to useful roles.
        raw = None
        # Prefer the concrete displayable. Ren'Py's Focus wrapper may expose a
        # generic ``kind='focus'`` marker which is less useful than the button
        # or input class that actually receives the click.
        for owner in (widget, focus):
            if owner is None:
                continue
            for attr_name in ("role", "kind", "widget_type", "displayable_type", "type"):
                value = getattr(owner, attr_name, None)
                if value is not None and not callable(value):
                    if str(value).casefold() in ("", "focus", "default"):
                        continue
                    raw = value
                    break
            if raw is not None:
                break
        if raw is None:
            raw = getattr(getattr(widget, "__class__", None), "__name__", "focus")
        try:
            name = str(raw)
        except Exception:
            name = "focus"
        lowered = name.casefold()
        for marker, role in (
            ("button", "button"),
            ("input", "input"),
            ("bar", "bar"),
            ("viewport", "viewport"),
            ("image", "image"),
            ("text", "text"),
        ):
            if marker in lowered:
                return role
        return name or "focus"

    def _renforge_focus_enabled(focus, widget):
        for owner in (focus, widget):
            if owner is None:
                continue
            for attr_name in ("enabled", "sensitive", "is_sensitive"):
                value = getattr(owner, attr_name, None)
                if value is None:
                    continue
                try:
                    value = value() if callable(value) else value
                except Exception:
                    continue
                return bool(value)
        return True

    def _renforge_focus_zorder(focus, widget, ordinal):
        for owner in (widget, focus):
            if owner is None:
                continue
            for attr_name in ("zorder", "z", "layer_zorder"):
                value = getattr(owner, attr_name, None)
                if value is None or callable(value):
                    continue
                try:
                    return int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
        # Focus list order is bottom→top in Ren'Py; higher index wins hits.
        return ordinal

    def _renforge_bounds_contain(bounds, x, y):
        try:
            left = int(bounds.get("x", 0))
            top = int(bounds.get("y", 0))
            width = int(bounds.get("width", 0))
            height = int(bounds.get("height", 0))
        except (TypeError, ValueError, OverflowError, AttributeError):
            return False
        return left <= x < left + width and top <= y < top + height

    def _renforge_engine_modal_blocked(owner, inventory):
        """Whether a modal screen hides ``owner``, by Ren'Py's own rule.

        ``Render.take_focuses`` walks renders in draw order; a modal render
        drops every focus collected before it (a ``"window"`` modal only those
        inside its rectangle). Apply that to where ``owner`` was drawn. A
        widget drawn in several places is hidden only if every copy is.
        Returns None when there is no completed render to judge from.
        """
        geometry = inventory["geometry"]
        if not geometry.get("available"):
            return None
        occurrences = geometry["occurrences"].get(id(owner)) or ()
        if not occurrences:
            return None
        modals = geometry.get("modals") or ()

        def inside(inner, outer):
            return (inner is not None and outer is not None
                    and inner["x"] >= outer["x"] and inner["y"] >= outer["y"]
                    and inner["x"] + inner["width"] <= outer["x"] + outer["width"]
                    and inner["y"] + inner["height"] <= outer["y"] + outer["height"])

        def hidden(occurrence):
            path = tuple(occurrence.get("render_path") or ())
            for modal_path, kind, rect in modals:
                # Drawn before it, an ancestor of it, or inside it: no effect.
                if modal_path <= path or modal_path[:len(path)] == path:
                    continue
                if kind == "full" or inside(occurrence.get("bounds"), rect):
                    return True
            return False

        return all(hidden(item) for item in occurrences)

    def _renforge_render_hit(x, y):
        """What a real click would focus at this point.

        Returns ``"unavailable"`` when the last screen render is missing,
        ``"none"`` when the point hits nothing, ``"modal"`` when a modal
        render grabs the point, or ``("displayable", widget)``.
        """
        render_mod = getattr(getattr(renpy, "display", None), "render", None)
        screen_render = getattr(render_mod, "screen_render", None)
        if screen_render is None:
            return "unavailable"
        method = getattr(screen_render, "focus_at_point", None)
        if not callable(method):
            return "unavailable"
        try:
            hit = method(int(x), int(y), None)
        except Exception as exc:
            if type(exc).__name__ == "IgnoreLayers":
                return "modal"
            return "unavailable"
        if hit is None:
            return "none"
        modal = getattr(render_mod, "Modal", None)
        if modal is not None and hit is modal:
            return "modal"
        if isinstance(hit, tuple) and hit:
            return ("displayable", hit[0])
        widget = getattr(hit, "widget", None)
        if widget is not None:
            return ("displayable", widget)
        return "none"

    # Where a click is tried on a control, in order: the center, then the
    # corners and edge midpoints, for a control partly behind another screen
    # or with a transparent middle. Probing stops at the first point that
    # reaches the control, so most controls cost one engine hit test.
    _RENFORGE_PROBE_POINTS = ((0.5, 0.5), (0.1, 0.1), (0.9, 0.1), (0.1, 0.9),
                              (0.9, 0.9), (0.5, 0.1), (0.5, 0.9), (0.1, 0.5), (0.9, 0.5))

    # Hit tests for one grid over the focus-masked buttons that no probe
    # reached (a full-screen button whose mask is one photo). It runs once per
    # interaction and is cached; on a 1920x1080 screen it tests a point about
    # every 55 px. One hit test costs about 50 microseconds.
    _RENFORGE_MASK_SCAN_POINTS = 700


    def _renforge_point_candidates(bounds):
        """The probe points inside ``bounds``, in ``_RENFORGE_PROBE_POINTS`` order."""
        if not bounds or bounds.get("width", 0) <= 0 or bounds.get("height", 0) <= 0:
            return []
        x, y, w, h = (bounds[key] for key in ("x", "y", "width", "height"))
        return [(int(x+min(w-1, w*px)), int(y+min(h-1, h*py))) for px, py in _RENFORGE_PROBE_POINTS]


    def _renforge_engine_focus_ids():
        """``id()`` of every widget in Ren'Py's own focus list."""
        focus_module = getattr(renpy.display, "focus", None)
        return {id(getattr(focus, "widget", None))
                for focus in getattr(focus_module, "focus_list", ()) or ()}


    def _renforge_has_focus_mask(widget):
        """Whether the widget's style sets a focus mask (only its opaque part is clickable)."""
        try:
            mask = getattr(getattr(widget, "style", None), "focus_mask", None)
        except Exception:
            return False
        return mask is not None and mask is not False


    def _renforge_scan_focus_masks(pending):
        """Find where focus-masked buttons can be clicked.

        A focus mask makes only the opaque part of a button clickable (a photo
        on a full-screen board), so the fixed probes can miss it. One grid of
        engine hit tests over their bounds finds each button's region.
        """
        # Scan once per interaction: observing again (as act does while it
        # waits) reuses what this interaction's scan found.
        bridge = _renforge_runtime.bridge
        generation = getattr(bridge, "observe_generation", None)
        cache = getattr(_renforge_runtime, "mask_scan", None)
        if not cache or cache[0] != generation:
            cache = (generation, {})
            _renforge_runtime.mask_scan = cache
        known = cache[1]
        fresh = []
        for widget, element in pending:
            key = (id(widget), tuple(element["bounds"][k] for k in ("x", "y", "width", "height")))
            if key in known:
                if known[key] is not None:
                    element.update(known[key])
                    element.pop("coverage_reason", None)
            else:
                fresh.append((widget, element, key))
        if not fresh:
            return
        pending = [(widget, element) for widget, element, _key in fresh]
        boxes = [element["bounds"] for _widget, element in pending]
        x0 = min(box["x"] for box in boxes)
        y0 = min(box["y"] for box in boxes)
        x1 = max(box["x"] + box["width"] for box in boxes)
        y1 = max(box["y"] + box["height"] for box in boxes)
        step = max(8, int(((x1 - x0) * (y1 - y0) / float(_RENFORGE_MASK_SCAN_POINTS)) ** 0.5) + 1)
        found = {id(widget): [] for widget, _element in pending}
        for y in range(y0 + step // 2, y1, step):
            for x in range(x0 + step // 2, x1, step):
                hit = _renforge_render_hit(x, y)
                if hit == "unavailable":
                    return
                if isinstance(hit, tuple) and id(hit[1]) in found:
                    found[id(hit[1])].append((x, y))
        for widget, element, key in fresh:
            points = found[id(widget)]
            known[key] = None
            if not points:
                continue
            cx = sum(x for x, _y in points) / float(len(points))
            cy = sum(y for _x, y in points) / float(len(points))
            px, py = min(points, key=lambda point: (point[0] - cx) ** 2 + (point[1] - cy) ** 2)
            # The region the mask leaves clickable, not the whole button. A
            # grid step out from the outermost hits encloses it.
            left = max(element["bounds"]["x"], min(x for x, _y in points) - step)
            top = max(element["bounds"]["y"], min(y for _x, y in points) - step)
            right = min(element["bounds"]["x"] + element["bounds"]["width"], max(x for x, _y in points) + step)
            bottom = min(element["bounds"]["y"] + element["bounds"]["height"], max(y for _x, y in points) + step)
            element["bounds"] = {"x": left, "y": top, "width": right - left, "height": bottom - top}
            element["click_point"] = {"x": px, "y": py}
            element["covered"] = False
            element["clickable"] = bool(element.get("enabled", True)) and element.get("visible") is not False
            element.pop("coverage_reason", None)
            known[key] = {name: element[name] for name in ("bounds", "click_point", "covered", "clickable")}


    def _renforge_mark_coverage(elements, inventory=None):
        """Ask the engine's click hit test which controls a click would reach.

        ``focus_at_point`` applies layer order, modal screens, and focus masks.
        Without a completed render, the engine focus list, which already drops
        controls behind a full modal, is the remaining evidence.
        """
        focusable = _renforge_engine_focus_ids()
        masked = []
        for focus, element in elements:
            widget = getattr(focus, "widget", None)
            enabled = bool(element.get("enabled", True))
            if element.get("semantic"):
                # Semantic operations bypass the pointer, so a probe cannot
                # tell whether a modal screen above blocks them.
                owner = widget if widget is not None else getattr(focus, "screen", None)
                blocked = (_renforge_engine_modal_blocked(owner, inventory)
                           if inventory is not None and owner is not None else None)
                element["covered"] = bool(blocked)
                element["clickable"] = enabled and not blocked
                continue
            hits = []
            point = None
            for candidate in _renforge_point_candidates(element.get("bounds")):
                hit = _renforge_render_hit(*candidate)
                hits.append(hit)
                if isinstance(hit, tuple) and hit[1] is widget:
                    point = candidate
                    break
                if hit == "unavailable":
                    break
            if point is not None:
                element["click_point"] = {"x": point[0], "y": point[1]}
                covered = False
            elif "modal" in hits and not (
                    # A miss inside the widget's own modal screen: the screen
                    # grabbed a point the widget's focus mask left open
                    # (full-screen masked buttons).
                    widget is not None and inventory is not None
                    and _renforge_engine_modal_blocked(widget, inventory) is False):
                covered = True
            elif not hits or hits[0] == "unavailable":
                covered = False if widget is None or id(widget) in focusable else None
            elif not enabled:
                covered = False
            else:
                covered = None
                element["coverage_reason"] = "no eligible point proven by bounded focus probes"
                if _renforge_has_focus_mask(widget):
                    masked.append((widget, element))
            element["covered"] = covered
            element["clickable"] = enabled and element.get("visible") is not False and covered is False
        if masked:
            _renforge_scan_focus_masks(masked)
        return elements


    def _renforge_explicit_focus_id(focus, widget):
        for owner in (focus, widget):
            if owner is None:
                continue
            for attr_name in ("mcp_id", "id", "widget_id", "focus_id", "name", "key"):
                value = getattr(owner, attr_name, None)
                if value is None or callable(value):
                    continue
                try:
                    value = str(value).strip()
                except Exception:
                    continue
                if value:
                    return value
        return None

    def _renforge_focus_action_name(focus, widget):
        """Action class name, as UI-element ids (``screen.Action``) use it."""
        for owner in (widget, focus):
            if owner is None:
                continue
            for attr_name in ("action", "clicked", "alternate", "hovered"):
                value = getattr(owner, attr_name, None)
                if value is None:
                    continue
                if isinstance(value, (builtins.list, tuple)) and len(value) == 1:
                    value = value[0]
                try:
                    name = getattr(value.__class__, "__name__", None)
                    if name and name not in ("list", "tuple", "object", "RevertableList"):
                        return str(name)
                    return str(value)
                except Exception:
                    continue
        return None

    def _renforge_ui_element_ids(pairs):
        """Give each control its UI-element id, keeping observe's id in ``control_id``.

        list_ui_elements, click_element, hover_element, scene_tree, and
        hit_test name a control by an explicit id, else the screen's widget
        name, else screen.Action, else screen.text, else screen:role:text,
        with "#2", "#3" on repeats. observe and act use the screen/... id
        kept in ``control_id``.
        """
        used = {}
        for ordinal, (focus, record) in enumerate(pairs):
            widget = getattr(focus, "widget", None)
            screen = record.get("screen")
            text = record.get("text")
            role = record.get("role") or record.get("type")
            element_id = _renforge_explicit_focus_id(focus, widget) or record.get("widget_id")
            if not element_id:
                action = _renforge_focus_action_name(focus, widget)
                if screen and action:
                    element_id = "%s.%s" % (screen, action)
                elif screen and text:
                    element_id = "%s.%s" % (screen, text)
                else:
                    element_id = "%s:%s:%s" % (screen or "screen", role, text or ordinal)
            element_id = _renforge_scene_string(element_id, _RENFORGE_SCENE_MAX_ID_CHARS)
            count = used.get(element_id, 0)
            used[element_id] = count + 1
            if count:
                element_id = "%s#%s" % (element_id, count + 1)
            record["control_id"] = record.get("id")
            record["id"] = element_id
        return pairs

    def _renforge_focusable_elements(max_items=None, max_text_chars=None, inventory=None):
        """``(focus, element)`` pairs for every current control, with UI-element ids.

        ``max_items`` keeps the first ones; ``max_text_chars`` truncates text
        fields (the raw type and screen stay in ``_raw_type`` and
        ``_raw_screen``). ``inventory`` reuses a scene inventory already built.
        """
        pairs, _truncated = _renforge_observe_focus_pairs(inventory)
        _renforge_ui_element_ids(pairs)
        if max_items is not None:
            pairs = pairs[:max_items]
        if max_text_chars is not None:
            for _focus, record in pairs:
                record["_raw_type"] = record.get("type")
                record["_raw_screen"] = record.get("screen")
                for key in ("text", "screen", "type", "action"):
                    record[key] = _renforge_scene_string(record.get(key), max_text_chars)
        return pairs

    def _renforge_focusable_choices():
        # Keep the historical choices API (text + compact index) unchanged;
        # generic UI enumeration above is intentionally broader and includes
        # controls without text.
        choices = []
        for focus, element in _renforge_focusable_elements():
            text = element.get("text")
            if text:
                choices.append((focus, text, element.get("screen")))
        return choices

    def _renforge_h_list_choices(payload):
        return {
            "choices": [
                {"index": index, "text": text, "screen": screen}
                for index, (_focus, text, screen) in enumerate(_renforge_focusable_choices())
            ]
        }


    def _renforge_h_list_menu_choices(payload):
        result = []
        index = 0
        for _focus, element in _renforge_focusable_elements():
            if not element.get("text"):
                continue
            if element.get("menu_index") is not None:
                result.append({"index": index, "text": element["text"], "screen": element.get("screen")})
            index += 1
        return {"choices": result}

    def _renforge_h_list_ui_elements(payload):
        payload = payload or {}
        requested_screen = payload.get("screen")
        requested_text = payload.get("text")
        requested_type = payload.get("type", payload.get("element_type"))
        if requested_screen is not None:
            requested_screen = str(requested_screen).casefold()
        if requested_text is not None:
            requested_text = str(requested_text).casefold()
        if requested_type is not None:
            requested_type = str(requested_type).casefold()

        elements = []
        for _focus, element in _renforge_focusable_elements():
            if requested_screen and str(element.get("screen") or "").casefold() != requested_screen:
                continue
            if requested_type:
                kind = str(element.get("type") or "").casefold()
                role = str(element.get("role") or "").casefold()
                if requested_type not in (kind, role):
                    continue
            if requested_text:
                text = str(element.get("text") or "").casefold()
                if requested_text not in text:
                    continue
            elements.append(element)
        result = {"elements": elements}
        try:
            frame = renpy.screenshot_to_bytes(None)
            result["frame_id"] = hashlib.sha256(frame).hexdigest()
            width = getattr(renpy.config, "screen_width", None)
            height = getattr(renpy.config, "screen_height", None)
            if width and height:
                result["screenshot"] = {"width": int(width), "height": int(height)}
        except Exception:
            pass
        return result

    def _renforge_is_end_interaction(exc):
        end_interaction = getattr(
            getattr(getattr(renpy, "display", None), "core", None),
            "EndInteraction",
            None,
        )
        return end_interaction is not None and isinstance(exc, end_interaction)


    def _renforge_is_script_control_flow(exc):
        """Is `exc` a screen action steering the script rather than failing?

        `Jump()` and `Call()` signal by raising, exactly like a button that
        returns a value raises EndInteraction. The drain runs on the main
        thread inside `ui.interact`, so these have to reach the context loop;
        caught there, a working button becomes a silent no-op reported as an
        error.
        """
        if _renforge_is_end_interaction(exc):
            return True
        game = getattr(renpy, "game", None)
        # Start on the main menu raises JumpOutException, which leaves the
        # menu context and then becomes a JumpException. Swallowing it leaves
        # the game on the menu and reports a failed click.
        # Loading, restarting, and quitting work by raising these. Before 8.2
        # there is no invoke_in_main_thread, so a load runs inside the drain.
        for name in ("JumpException", "JumpOutException", "CallException",
                     "RestartContext", "RestartTopContext", "FullRestartException",
                     "UtterRestartException", "QuitException"):
            signal = getattr(game, name, None)
            if isinstance(signal, type) and isinstance(exc, signal):
                return True
        return False


    def _renforge_reset_testmouse_state():
        testmouse = getattr(getattr(renpy, "test", None), "testmouse", None)
        if testmouse is None:
            return
        try:
            setattr(testmouse, "mouse_pos", None)
        except Exception:
            pass
        mouse_buttons = getattr(testmouse, "mouse_buttons", None)
        if mouse_buttons is None:
            return
        if isinstance(mouse_buttons, dict):
            for key in list(mouse_buttons.keys()):
                mouse_buttons[key] = False
            return
        if isinstance(mouse_buttons, tuple):
            mouse_buttons = list(mouse_buttons)
            setattr(testmouse, "mouse_buttons", mouse_buttons)
        if isinstance(mouse_buttons, list):
            for index in range(len(mouse_buttons)):
                try:
                    mouse_buttons[index] = False
                except Exception:
                    mouse_buttons[index] = 0


    def _renforge_dispatch_mouse_motion(px, py):
        if pygame is None:
            return False
        event_type = getattr(pygame, "MOUSEMOTION", None)
        event_factory = getattr(getattr(pygame, "event", None), "Event", None)
        post = getattr(getattr(pygame, "event", None), "post", None)
        if event_type is None or not callable(event_factory):
            return False
        payload = {"pos": (px, py), "rel": (0, 0), "buttons": (0, 0, 0)}
        try:
            event = event_factory(event_type, **payload)
        except TypeError:
            event = event_factory(event_type, payload)
        if callable(post):
            post(event)
        mouse_handler = getattr(
            getattr(getattr(renpy, "display", None), "focus", None), "mouse_handler", None
        )
        if callable(mouse_handler):
            mouse_handler(event, px, py, False)
        return True

    def _renforge_dispatch_mouse_click(x, y, button=1):
        if pygame is None:
            return False
        event_factory = getattr(getattr(pygame, "event", None), "Event", None)
        if event_factory is None:
            return False
        event_type = getattr(pygame, "MOUSEBUTTONDOWN", None)
        up_type = getattr(pygame, "MOUSEBUTTONUP", None)
        if event_type is None or up_type is None:
            return False
        payload = {
            "button": button,
            "pos": (x, y),
            "x": x,
            "y": y,
            "touch": False,
            "test": True,
            "mod": 0,
        }
        try:
            down = event_factory(event_type, **payload)
            up = event_factory(up_type, **payload)
        except TypeError:
            down = event_factory(event_type, payload)
            up = event_factory(up_type, payload)
        focus_module = getattr(getattr(renpy, "display", None), "focus", None)
        mouse_handler = getattr(focus_module, "mouse_handler", None)
        get_focused = getattr(focus_module, "get_focused", None)
        if not callable(mouse_handler) or not callable(get_focused):
            return False
        mouse_handler(down, x, y, False)
        focused = get_focused()
        event_handler = getattr(focused, "event", None)
        if not callable(event_handler):
            return False
        local_x, local_y = x, y
        owner = None
        for focus in reversed(list(getattr(focus_module, "focus_list", []) or [])):
            if getattr(focus, "widget", None) is not focused:
                continue
            owner = focus
            focus_x = getattr(focus, "x", None)
            focus_y = getattr(focus, "y", None)
            if focus_x is not None and focus_y is not None:
                local_x, local_y = x - focus_x, y - focus_y
            break
        ignore_event = getattr(getattr(renpy, "display", None), "core", None)
        ignore_event = getattr(ignore_event, "IgnoreEvent", None)
        no_interaction_result = object()

        def _deliver():
            delivered = no_interaction_result
            for event in (down, up):
                try:
                    rv = event_handler(event, local_x, local_y, 0)
                    if rv is not None and delivered is no_interaction_result:
                        delivered = rv
                except Exception as exc:
                    if ignore_event is None or not isinstance(exc, ignore_event):
                        raise
            return delivered

        interaction_result = _renforge_with_focus_screen(owner, _deliver)
        if interaction_result is not no_interaction_result:
            end_interaction = getattr(renpy, "end_interaction", None)
            if callable(end_interaction):
                end_interaction(interaction_result)
        return True

    def _renforge_click_pointer(x, y):
        interface = getattr(getattr(renpy, "display", None), "interface", None)
        if interface is not None:
            try:
                interface.mouse_focused = True
            except Exception:
                pass
            try:
                interface.ignore_touch = False
            except Exception:
                pass

        _renforge_reset_testmouse_state()
        try:
            _renforge_dispatch_mouse_motion(x, y)
            if _renforge_dispatch_mouse_click(x, y):
                return "renpy"

            testmouse = getattr(getattr(renpy, "test", None), "testmouse", None)
            click_mouse = getattr(testmouse, "click_mouse", None)
            if callable(click_mouse):
                bridge = _renforge_runtime.bridge
                move_mouse = getattr(testmouse, "move_mouse", None)
                request = getattr(bridge, "current_request", None)
                if pygame is not None and callable(move_mouse) and request is not None:
                    # Creator-defined displayables may calculate hover in render,
                    # then consult it on mouse-up. Keep the synthetic position
                    # through a frame before delivering the click. Defer the RPC
                    # reply until cleanup so a following observation cannot race it.
                    move_mouse(x, y)
                    now = _renforge_time.monotonic()
                    bridge.pointer_click = {
                        "request": request, "position": (x, y), "phase": "hover",
                        "due": now + 0.1, "deadline": now + 0.75,
                        "generation": bridge.observe_generation,
                    }
                else:
                    click_mouse(1, x, y)
                return "renpy-test"
            raise RuntimeError("Ren'Py synthetic mouse API is unavailable")
        finally:
            if getattr(_renforge_runtime.bridge, "pointer_click", None) is None:
                _renforge_reset_testmouse_state()

    def _renforge_pointer_tick(cancel=False):
        """Advance a pending pointer click by one main-thread tick.

        A click is hovered first, then pressed once its due time comes, and
        the waiting request is answered when it finishes, times out, the
        interaction changes, or ``cancel`` is set. Returns True while a click
        is still pending.
        """
        bridge = _renforge_runtime.bridge
        pending = getattr(bridge, "pointer_click", None)
        if pending is None:
            return False
        now = _renforge_time.monotonic()
        request = pending["request"]
        finished = cancel or now >= pending["deadline"]
        try:
            if finished:
                if pending["phase"] == "hover":
                    request.error = "pointer click cancelled before delivery"
            elif now >= pending["due"]:
                if pending["phase"] == "hover":
                    if bridge.observe_generation != pending["generation"]:
                        request.error = "pointer interaction changed before delivery"
                        finished = True
                    else:
                        x, y = pending["position"]
                        renpy.test.testmouse.click_mouse(1, x, y)
                        pending["phase"] = "clicked"
                        pending["due"] = now + 0.1
                else:
                    finished = True
        except Exception as exc:
            request.error = "%s: %s" % (type(exc).__name__, exc)
            finished = True
        finally:
            if finished:
                bridge.pointer_click = None
                _renforge_reset_testmouse_state()
                request.event.set()
        return not finished

    def _renforge_custom_action_attr(widget):
        """The attribute a game's own displayable keeps its click action in.

        ``action`` when present, else the one attribute whose name mentions
        "click" (``click_action``, ``on_click``). Several such attributes are
        ambiguous, so none is chosen.
        """
        if hasattr(widget, "action"):
            return "action"
        try:
            names = builtins.sorted(key for key in vars(widget)
                                    if isinstance(key, builtins.str) and "click" in key.lower())
        except TypeError:
            return None
        return names[0] if len(names) == 1 else None

    def _renforge_custom_actions(widget):
        """``(value, actions)`` from a game's own displayable's click attribute, or None.

        The attribute (see ``_renforge_custom_action_attr``) counts only when
        everything it holds is an action: a Ren'Py Action when
        ``renpy.ui.Action`` exists, else a callable. A counter or flag such
        as ``click_count = 0`` is not an action, so this returns None. An
        attribute holding nothing (``action = None``) gives ``(value, [])``:
        control discovery lists that widget as a disabled button, as Ren'Py
        does a button with no action, while a click has nothing to run.
        """
        attr = _renforge_custom_action_attr(widget)
        if attr is None:
            return None
        value = getattr(widget, attr, None)
        actions = _renforge_action_list(value)
        action_type = getattr(getattr(renpy, "ui", None), "Action", None)
        if isinstance(action_type, type):
            is_action = lambda item: isinstance(item, action_type)
        else:
            is_action = callable
        return (value, actions) if all(is_action(item) for item in actions) else None

    def _renforge_custom_action_value(widget):
        """The click action a game's own displayable keeps, or None.

        Only an attribute that holds one or more actions counts (see
        ``_renforge_custom_actions``); otherwise the caller clicks the pointer
        instead of running it.
        """
        found = _renforge_custom_actions(widget)
        return found[0] if found is not None and found[1] else None

    def _renforge_focus_action_value(focus):
        # Button.per_interact copies a sensitive action onto clicked, and
        # Button.event runs clicked. Prefer that, then the declared action.
        menu_action = getattr(focus, "menu_action", None)
        if menu_action is not None:
            return menu_action
        # A game's own displayable listed by discovery carries the action
        # discovery validated; its ``clicked`` may be a press flag, not one.
        if hasattr(focus, "custom_action"):
            return focus.custom_action
        widget = getattr(focus, "widget", None)
        for owner in (widget, focus):
            if owner is None:
                continue
            clicked = getattr(owner, "clicked", None)
            if clicked is not None:
                return clicked
            action = getattr(owner, "action", None)
            if action is not None:
                return action
        # A game's own displayable may keep it under a click attribute.
        if widget is not None and not type(widget).__module__.startswith("renpy."):
            return _renforge_custom_action_value(widget)
        return None

    def _renforge_with_focus_screen(focus, fn):
        """Run ``fn`` with the focus's screen pushed, then pop it.

        ``ScreenDisplayable.event`` does this before a button sees the click.
        ``Hide()`` and other actions read ``renpy.current_screen()``, which is
        empty when the widget event or ``behavior.run`` is called directly.
        """
        screen_mod = getattr(getattr(renpy, "display", None), "screen", None)
        screen = getattr(focus, "screen", None) if focus is not None else None
        push = getattr(screen_mod, "push_current_screen", None) if screen_mod is not None else None
        pop = getattr(screen_mod, "pop_current_screen", None) if screen_mod is not None else None
        if screen is None or not callable(push) or not callable(pop):
            return fn()
        push(screen)
        try:
            return fn()
        finally:
            pop()

    def _renforge_invoke_focus_action(focus):
        """Run the focus action the way Button.handle_click does.

        Returns True when an action object was run. Returns False when there
        is nothing to run, so the caller can click the rectangle instead.
        A returned value ends the interaction. Jump and EndInteraction
        propagate. The action runs with the focus's screen pushed.
        """
        action = _renforge_focus_action_value(focus)
        if action is None:
            return False
        behavior = getattr(getattr(renpy, "display", None), "behavior", None)
        run = getattr(behavior, "run", None)
        if not callable(run):
            run = getattr(renpy, "run", None)
        if not callable(run):
            return False

        def _run():
            rv = run(action)
            if rv is not None:
                end_interaction = getattr(renpy, "end_interaction", None)
                if callable(end_interaction):
                    end_interaction(rv)
            return True

        return _renforge_with_focus_screen(focus, _run)

    def _renforge_focus_point(focus, element=None):
        """Where the pointer goes for a control, for a click or a hover.

        The point the coverage probes or mask scan proved reaches it, else
        the centre of its bounds, else of its focus rectangle, else Ren'Py's
        test position for the focus.
        """
        if element is not None and element.get("click_point"):
            return element["click_point"]["x"], element["click_point"]["y"]
        if element is not None and element.get("bounds"):
            bounds = element["bounds"]
            return int(bounds["x"] + bounds["width"] // 2), int(bounds["y"] + bounds["height"] // 2)
        fx = getattr(focus, "x", None)
        fy = getattr(focus, "y", None)
        fw = getattr(focus, "w", None)
        fh = getattr(focus, "h", None)
        if fx is not None and fy is not None and fw and fh:
            return int(fx + fw // 2), int(fy + fh // 2)
        find_position = getattr(getattr(renpy, "test", None), "testfocus", None)
        find_position = getattr(find_position, "find_position", None)
        if not callable(find_position):
            raise RuntimeError("Ren'Py focus position API is unavailable")
        px, py = find_position(focus, (None, None))
        return int(px), int(py)

    def _renforge_click_focus(focus, element=None):
        """Click a control at ``_renforge_focus_point`` through the shared pointer path."""
        x, y = _renforge_focus_point(focus, element)
        try:
            _renforge_click_pointer(x, y)
        except Exception as exc:
            if not _renforge_is_end_interaction(exc):
                raise
            setattr(exc, "renforge_pointer", (x, y))
            raise
        return x, y

    def _renforge_resolve_ui_element(payload, action, elements=None):
        """Find the one UI element ``payload`` names by id and/or text.

        Returns ``(focus, element, None)``, or ``(None, None, error reply)``.
        ``elements`` are ``_renforge_focusable_elements()`` pairs a caller
        already built for this request; None builds them.
        """
        payload = payload or {}
        wanted_id = payload.get("id") or payload.get("element_id")
        wanted_text = payload.get("text")
        if wanted_text == "":
            wanted_text = None
        exact = bool(payload.get("exact", False))
        wanted_screen = payload.get("screen")
        if wanted_id is None and wanted_text is None:
            return None, None, {"ok": False, "error": "%s requires text or id" % action}
        if wanted_id is not None:
            wanted_id = str(wanted_id)
        if wanted_text is not None:
            wanted_text = str(wanted_text)
        if wanted_screen is not None:
            wanted_screen = str(wanted_screen).casefold()

        if elements is None:
            elements = _renforge_focusable_elements()
        candidates = []
        for focus, element in elements:
            if wanted_screen and str(element.get("screen") or "").casefold() != wanted_screen:
                continue
            # UI-element ids, widget names, and observe's control ids all resolve.
            if (wanted_id is not None and str(element.get("id")) != wanted_id
                    and element.get("widget_id") != wanted_id
                    and element.get("control_id") != wanted_id):
                continue
            if wanted_text is not None:
                actual_text = str(element.get("text") or "")
                matches = actual_text.casefold() == wanted_text.casefold() if exact else wanted_text.casefold() in actual_text.casefold()
                if not matches:
                    continue
            candidates.append((focus, element))
        if not candidates:
            return None, None, {"ok": False, "error": "no UI element matching %r/%r" % (wanted_text, wanted_id)}
        if len(candidates) > 1:
            return None, None, {
                "ok": False,
                "error": "ambiguous UI element; provide an id or exact text",
                "matches": [item[1] for item in candidates],
            }
        return candidates[0][0], candidates[0][1], None

    def _renforge_h_click_element(payload):
        payload = payload or {}
        expected_frame_id = payload.get("expected_frame_id") or payload.get("expected_screenshot")
        # One scene walk serves the lookup and the hit stack after the click.
        elements = _renforge_focusable_elements()
        focus, element, error = _renforge_resolve_ui_element(payload, "click_element", elements)
        if error is not None:
            return error

        screenshot_digest = None
        if expected_frame_id not in (None, ""):
            data = renpy.screenshot_to_bytes(None)
            matches, screenshot_digest = _renforge_screenshot_guard_matches(expected_frame_id, data)
            if not matches:
                return {
                    "ok": False,
                    "error": "expected_frame_id guard failed",
                    "sha256": screenshot_digest,
                }
        if not element.get("enabled", True):
            return {"ok": False, "error": "UI element is disabled", "element": element}
        if element.get("covered") or element.get("clickable") is False:
            return {"ok": False, "error": "UI element has no eligible click", "element": element}
        if element.get("semantic") and "click" in element.get("operations", ()):
            # Running the action needs a settled frame, as act does. Only a
            # refusal pays for a second scene walk: its reply carries the
            # observation.
            stable, _reason = _renforge_observe_stability()
            if stable is not True:
                return {"ok": False, "error": "unstable",
                        "observation": _renforge_observe_raw(False, include_readout=False)}
            result = {"ok": True, "id": element["id"], "text": element.get("text"),
                      "screen": element.get("screen"), "bounds": element.get("bounds"), "via": "action"}
            try:
                if not _renforge_invoke_focus_action(focus):
                    return {"ok": False, "error": "unsupported"}
            except Exception as exc:
                if not _renforge_is_script_control_flow(exc):
                    raise
                setattr(exc, "renforge_result", result)
                raise
            return result
        pending_end_interaction = None
        try:
            x, y = _renforge_click_focus(focus, element)
        except Exception as exc:
            if not _renforge_is_end_interaction(exc):
                raise
            pending_end_interaction = exc
            x, y = getattr(exc, "renforge_pointer")
        # Report which focusable actually owns this coordinate (coverage).
        hit = _renforge_hit_stack(x, y, elements)
        topmost = hit.get("topmost")
        action_name = element.get("action")
        # Native quick-menu actions often run as a result of the click; emit a
        # correlated business event when the action name is recognizable so
        # wait_for_effect can resolve without inspecting files.
        if action_name:
            lowered = str(action_name).casefold()
            if "quicksave" in lowered or lowered == "quick_save":
                slot = _renforge_newest_quick_slot()
                _renforge_emit_business("quick_save.completed", slot=slot, source="click_element")
            elif "quickload" in lowered or lowered == "quick_load":
                slot = _renforge_newest_quick_slot()
                bridge = _renforge_runtime.bridge
                _renforge_emit_business(
                    "quick_load.completed",
                    slot=slot,
                    restored_label=bridge.current_label if bridge is not None else None,
                    source="click_element",
                )
            elif "rollback" in lowered or lowered == "back":
                history = _renforge_history_index()
                _renforge_emit_business(
                    "rollback.completed",
                    from_history_index=history,
                    to_history_index=(history - 1) if history is not None else None,
                    source="click_element",
                )
            elif "skip" in lowered:
                bridge = _renforge_runtime.bridge
                if bridge is not None:
                    bridge._skip_reason_hint = "user_click"
                _renforge_emit_business("skip.changed", requested=True, source="click_element")
            elif "auto" in lowered or "afm" in lowered:
                _renforge_emit_business("auto.changed", requested=True, source="click_element")
        result = {
            "ok": True,
            "id": element.get("id"),
            "text": element.get("text"),
            "type": element.get("type"),
            "screen": element.get("screen"),
            "action": action_name,
            "bounds": element.get("bounds"),
            "x": x,
            "y": y,
            "coordinate_space": "logical",
            "element": element,
            "received_by": topmost,
        }
        if topmost is not None and topmost.get("id") != element.get("id"):
            result["warning"] = (
                "The intended button may be covered by another interactive displayable."
            )
        if screenshot_digest is not None:
            result["sha256"] = screenshot_digest
        if pending_end_interaction is not None:
            setattr(pending_end_interaction, "renforge_result", result)
            raise pending_end_interaction
        return result

    def _renforge_hit_stack(x, y, elements=None):
        """Return the focus a click would hit, plus other rectangles at the point.

        ``topmost`` is what Ren'Py's own hit test names. When the engine says
        the point hits nothing (a transparent part of a masked button), there
        is no receiver and the rectangles there are all ``underneath``. Only
        without a completed render does the topmost rectangle stand in.
        ``elements`` are focusable-element pairs already built for this
        request; None builds them.
        """
        if elements is None:
            elements = _renforge_focusable_elements()
        hit = _renforge_render_hit(x, y)
        hits = [(focus, element) for focus, element in elements
                if element.get("covered") is not True
                and _renforge_bounds_contain(element.get("bounds") or {}, x, y)]
        topmost = hits[-1][1] if hits and hit == "unavailable" else None
        if hit == "modal":
            hits = []
        elif isinstance(hit, tuple) and hit[0] == "displayable":
            topmost = hits[-1][1] if hits else None
            for focus, element in elements:
                if getattr(focus, "widget", None) is hit[1]:
                    topmost = element
                    break
        underneath = [element for _focus, element in reversed(hits) if element is not topmost]
        result = {
            "ok": True,
            "x": x,
            "y": y,
            "coordinate_space": "logical",
            "topmost": topmost,
            "underneath": underneath,
        }
        if topmost is not None and underneath:
            result["warning"] = "Multiple interactive displayables overlap this point."
        return result

    def _renforge_h_hit_test(payload):
        payload = payload or {}
        try:
            raw_x, raw_y = payload.get("x"), payload.get("y")
            if isinstance(raw_x, bool) or isinstance(raw_y, bool):
                raise ValueError
            x, y = int(round(float(raw_x))), int(round(float(raw_y)))
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "hit_test requires numeric x and y"}
        if x < 0 or y < 0:
            return {"ok": False, "error": "hit_test coordinates must be non-negative"}

        coordinate_space = str(payload.get("coordinate_space", "logical") or "logical").casefold()
        if coordinate_space not in ("logical", "screenshot"):
            return {"ok": False, "error": "coordinate_space must be logical or screenshot"}

        frame_data = None
        x, y, frame_data, coordinate_error = _renforge_to_logical_coordinates(
            x, y, coordinate_space, frame_data
        )
        if coordinate_error is not None:
            return {"ok": False, "error": coordinate_error}

        result = _renforge_hit_stack(x, y)
        result["coordinate_space"] = "logical"
        if coordinate_space == "screenshot":
            result["requested_coordinate_space"] = "screenshot"
        return result

    def _renforge_move_mouse(focus, element=None):
        """Hover a control at the point a click would use (``_renforge_focus_point``).

        Returns ``(x, y, method)``.
        """
        x, y = _renforge_focus_point(focus, element)

        interface = getattr(getattr(renpy, "display", None), "interface", None)
        if interface is not None:
            try:
                interface.mouse_focused = True
            except Exception:
                pass
            try:
                interface.ignore_touch = False
            except Exception:
                pass

        set_mouse_pos = getattr(renpy, "set_mouse_pos", None)
        testmouse = getattr(getattr(renpy, "test", None), "testmouse", None)
        move_mouse = getattr(testmouse, "move_mouse", None)
        restart_interaction = getattr(renpy, "restart_interaction", None)
        used_native_set_mouse = False

        _renforge_reset_testmouse_state()
        try:
            if callable(set_mouse_pos):
                try:
                    set_mouse_pos(x, y, duration=0)
                except TypeError:
                    try:
                        set_mouse_pos(x, y, 0)
                    except TypeError:
                        try:
                            set_mouse_pos(x, y)
                        except TypeError:
                            pass
                        else:
                            used_native_set_mouse = True
                    else:
                        used_native_set_mouse = True
                else:
                    used_native_set_mouse = True
                if used_native_set_mouse:
                    _renforge_dispatch_mouse_motion(x, y)
                    if callable(restart_interaction):
                        restart_interaction()
                    return x, y, "renpy"

            if callable(move_mouse):
                try:
                    move_mouse(x, y)
                except TypeError:
                    pass
                else:
                    _renforge_dispatch_mouse_motion(x, y)
                    if callable(restart_interaction):
                        restart_interaction()
                    return x, y, "renpy-test"

            if not _renforge_dispatch_mouse_motion(x, y):
                raise RuntimeError("hover unavailable: pygame mouse-motion API is unavailable")
            if callable(restart_interaction):
                restart_interaction()
            return x, y, "pygame"
        finally:
            _renforge_reset_testmouse_state()

    def _renforge_h_hover_element(payload):
        payload = payload or {}
        expected_frame_id = payload.get("expected_frame_id") or payload.get("expected_screenshot")
        focus, element, error = _renforge_resolve_ui_element(payload, "hover_element")
        if error is not None:
            return error
        screenshot_digest = None
        if expected_frame_id not in (None, ""):
            data = renpy.screenshot_to_bytes(None)
            matches, screenshot_digest = _renforge_screenshot_guard_matches(expected_frame_id, data)
            if not matches:
                return {
                    "ok": False,
                    "error": "expected_frame_id guard failed",
                    "sha256": screenshot_digest,
                }
        if not element.get("enabled", True):
            return {"ok": False, "error": "UI element is disabled", "element": element}
        # As for a click: a covered control would hover whatever covers it.
        if element.get("covered") or element.get("clickable") is False:
            return {"ok": False, "error": "UI element has no eligible click", "element": element}
        try:
            x, y, method = _renforge_move_mouse(focus, element)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "element": element}
        result = {
            "ok": True,
            "hovered": True,
            "method": method,
            "id": element.get("id"),
            "text": element.get("text"),
            "type": element.get("type"),
            "screen": element.get("screen"),
            "bounds": element.get("bounds"),
            "x": x,
            "y": y,
            "element": element,
        }
        if screenshot_digest is not None:
            result["sha256"] = screenshot_digest
        return result

    def _renforge_rect_components(rect):
        left = getattr(rect, "left", None)
        top = getattr(rect, "top", None)
        width = getattr(rect, "width", None)
        height = getattr(rect, "height", None)
        if left is None or top is None or width is None or height is None:
            try:
                left, top, width, height = rect[0], rect[1], rect[2], rect[3]
            except (TypeError, IndexError, ValueError):
                raise ValueError("unsupported rect type")
        return int(left), int(top), int(width), int(height)

    def _renforge_h_get_ui_element_bounds(payload):
        payload = payload or {}
        expected_frame_id = payload.get("expected_frame_id") or payload.get("expected_screenshot")
        inventory = _renforge_scene_inventory()
        focus, element, error = _renforge_resolve_ui_element(
            payload, "get_ui_element_bounds", _renforge_focusable_elements(inventory=inventory))
        if error is not None:
            return error
        screenshot_digest = None
        if expected_frame_id not in (None, ""):
            data = renpy.screenshot_to_bytes(None)
            matches, screenshot_digest = _renforge_screenshot_guard_matches(expected_frame_id, data)
            if not matches:
                return {
                    "ok": False,
                    "error": "expected_frame_id guard failed",
                    "sha256": screenshot_digest,
                }

        bounds = element.get("bounds")
        result = {
            "ok": True,
            "id": element.get("id"),
            "text": element.get("text"),
            "type": element.get("type"),
            "screen": element.get("screen"),
            "focus_bounds": bounds,
            "painted_bounds": None,
            "painted_bounds_available": False,
            "coordinate_space": "logical",
        }
        widget = getattr(focus, "widget", None)
        if widget is None or not hasattr(widget, "state_children") or not callable(getattr(widget, "get_child", None)):
            result["painted_bounds_reason"] = "element does not expose ImageButton state children"
        else:
            rendered = next((entry for entry in inventory["geometry"]["renders"].get(id(widget), [])
                             if entry[2]["visible"] is True), None)
            if rendered is None:
                result["painted_bounds_reason"] = "completed widget render unavailable"
            else:
                try:
                    render, matrix, occurrence = rendered
                    # render_to_surface accepts a Render on newer SDKs and
                    # rasterizes it directly. Older SDKs expose the same draw
                    # seam. Neither path invokes the displayable's render.
                    rasterize = getattr(renpy, "render_to_surface", None)
                    if callable(rasterize):
                        surface = rasterize(render, resize=True)
                    else:
                        surface = renpy.display.draw.screenshot(render)
                        size = (int(render.width), int(render.height))
                        if surface.get_size() != size:
                            surface = renpy.display.scale.smoothscale(surface, size)
                    if surface is None:
                        raise RuntimeError("completed render has no inspectable alpha surface")
                    rect = surface.get_bounding_rect(min_alpha=1)
                    left, top, painted_width, painted_height = _renforge_rect_components(rect)
                    if painted_width <= 0 or painted_height <= 0:
                        raise RuntimeError("completed render is fully transparent")
                    a, b, c, d, tx, ty = matrix
                    points = [(a*x+c*y+tx, b*x+d*y+ty) for x, y in
                              ((left, top), (left+painted_width, top),
                               (left+painted_width, top+painted_height), (left, top+painted_height))]
                    x = max(bounds["x"], _renforge_math.floor(min(p[0] for p in points)))
                    y = max(bounds["y"], _renforge_math.floor(min(p[1] for p in points)))
                    right = min(bounds["x"]+bounds["width"], _renforge_math.ceil(max(p[0] for p in points)))
                    bottom = min(bounds["y"]+bounds["height"], _renforge_math.ceil(max(p[1] for p in points)))
                    result["painted_bounds"] = {"x": x, "y": y, "width": max(0, right-x), "height": max(0, bottom-y)}
                    result["painted_bounds_available"] = True
                    result["painted_bounds_source"] = "completed-render-alpha"
                    result["state"] = str(getattr(getattr(widget, "style", None), "prefix", "")).rstrip("_") or None
                except Exception as exc:
                    result["painted_bounds_reason"] = "%s: %s" % (type(exc).__name__, exc)
        if screenshot_digest is not None:
            result["sha256"] = screenshot_digest
        return result

    def _renforge_state_matches(actual, expected):
        if isinstance(expected, builtins.dict):
            if not isinstance(actual, builtins.dict):
                return False
            for key, value in expected.items():
                if key not in actual or not _renforge_state_matches(actual[key], value):
                    return False
            return True
        if isinstance(expected, (list, tuple)):
            return isinstance(actual, (list, tuple)) and len(actual) == len(expected) and all(
                _renforge_state_matches(a, e) for a, e in zip(actual, expected)
            )
        return actual == expected

    def _renforge_screenshot_guard_matches(expected, data):
        digest = hashlib.sha256(data).hexdigest()
        if isinstance(expected, builtins.dict):
            expected = expected.get(
                "frame_id",
                expected.get(
                    "sha256",
                    expected.get("hash", expected.get("id", expected.get("base64"))),
                ),
            )
        if isinstance(expected, bytes):
            return expected == data, digest
        if not isinstance(expected, str) or not expected.strip():
            return False, digest
        value = expected.strip()
        if value.casefold().startswith("sha256:"):
            value = value.split(":", 1)[1].strip()
        if value.casefold() == digest.casefold():
            return True, digest
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception:
            decoded = None
        return decoded == data, digest

    def _renforge_png_dimensions(data):
        if not isinstance(data, bytes) or len(data) < 24:
            return None, None
        if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return None, None
        try:
            return struct.unpack(">II", data[16:24])
        except Exception:
            return None, None

    def _renforge_to_logical_coordinates(x, y, coordinate_space, frame_data=None, descriptor=None):
        """Convert screenshot pixels to logical coordinates (click_at, hit_test, scroll, drag)."""
        if coordinate_space == "logical":
            return x, y, frame_data, None
        if coordinate_space != "screenshot":
            return x, y, frame_data, "coordinate_space must be logical or screenshot"
        if frame_data is None:
            frame_data = renpy.screenshot_to_bytes(None)
        pixel_width, pixel_height = _renforge_png_dimensions(frame_data)
        logical_width = getattr(renpy.config, "screen_width", None)
        logical_height = getattr(renpy.config, "screen_height", None)
        if not pixel_width or not pixel_height or not logical_width or not logical_height:
            return x, y, frame_data, "screenshot coordinate space is unavailable"
        if descriptor is not None:
            pixel_width, pixel_height = descriptor["width"], descriptor["height"]
        if not (0 <= x < pixel_width and 0 <= y < pixel_height):
            return x, y, frame_data, "point is outside the screenshot"
        x = int(round(x * float(logical_width) / float(pixel_width)))
        y = int(round(y * float(logical_height) / float(pixel_height)))
        x, y = min(x, logical_width - 1), min(y, logical_height - 1)
        return x, y, frame_data, None

    def _renforge_h_click_at(payload):
        payload = payload or {}
        try:
            raw_x, raw_y = payload.get("x"), payload.get("y")
            if isinstance(raw_x, bool) or isinstance(raw_y, bool):
                raise ValueError
            x, y = float(raw_x), float(raw_y)
            if not _renforge_math.isfinite(x) or not _renforge_math.isfinite(y):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "click_at requires numeric x and y"}
        if x < 0 or y < 0:
            return {"ok": False, "error": "click_at coordinates must be non-negative"}

        coordinate_space = str(payload.get("coordinate_space", "logical") or "logical").casefold()
        if coordinate_space not in ("logical", "screenshot"):
            return {"ok": False, "error": "coordinate_space must be logical or screenshot"}

        expected_state = payload.get("expected_state")
        if expected_state is not None:
            state = _renforge_h_get_state({})
            if not _renforge_state_matches(state, expected_state):
                return {"ok": False, "error": "expected_state guard failed", "state": state}

        expected_screenshot = payload.get("expected_screenshot") or payload.get("expected_frame_id")
        screenshot_digest = None
        frame_data = None
        descriptor = expected_screenshot if isinstance(expected_screenshot, builtins.dict) and "width" in expected_screenshot else None
        if expected_screenshot not in (None, ""):
            frame_data = renpy.screenshot_to_bytes(None)
            if descriptor is not None:
                error = _renforge_runtime.interaction.validate_frame(
                    descriptor, session_id=_renforge_runtime.bridge.session_id,
                    logical_size=(renpy.config.screen_width, renpy.config.screen_height),
                    source_size=_renforge_png_dimensions(frame_data))
                if error:
                    return {"ok": False, "error": error}
            matches, screenshot_digest = _renforge_screenshot_guard_matches(expected_screenshot, frame_data)
            if not matches:
                return {
                    "ok": False,
                    "error": "expected_screenshot guard failed",
                    "sha256": screenshot_digest,
                }

        x, y, frame_data, coordinate_error = _renforge_to_logical_coordinates(
            x, y, coordinate_space, frame_data, descriptor
        )
        if coordinate_error is not None:
            return {"ok": False, "error": coordinate_error}
        x, y = int(round(x)), int(round(y))

        pending_end_interaction = None
        try:
            _renforge_click_pointer(x, y)
        except Exception as exc:
            if not _renforge_is_end_interaction(exc):
                raise
            pending_end_interaction = exc
        result = {"ok": True, "x": x, "y": y, "coordinate_space": coordinate_space}
        if screenshot_digest is not None:
            result["sha256"] = screenshot_digest
        if pending_end_interaction is not None:
            setattr(pending_end_interaction, "renforge_result", result)
            raise pending_end_interaction
        return result

    def _renforge_h_get_displayable_bounds(payload):
        # Report where a shown image tag was actually rendered, in Ren'Py
        # logical coordinates. This closes the pixel-perfect loop: instead of
        # eyeballing a sprite on a screenshot, a caller can measure its real
        # position and size after a show/reposition.
        payload = payload or {}
        tag = payload.get("tag")
        if not tag:
            return {"ok": False, "error": "get_displayable_bounds requires a tag"}
        tag = str(tag)
        layer = payload.get("layer")
        layer = str(layer) if layer else None
        get_bounds = getattr(renpy, "get_image_bounds", None)
        if not callable(get_bounds):
            return {"ok": False, "error": "renpy.get_image_bounds is unavailable"}
        try:
            if layer:
                bounds = get_bounds(tag, layer=layer)
            else:
                bounds = get_bounds(tag)
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        try:
            showing = list(renpy.get_showing_tags(layer)) if layer else list(renpy.get_showing_tags())
        except Exception:
            showing = []
        if not bounds:
            return {
                "ok": False,
                "error": "tag %r is not showing" % tag,
                "tag": tag,
                "showing": False,
                "showing_tags": showing,
            }
        x, y, w, h = bounds
        x, y, w, h = int(x), int(y), int(w), int(h)
        result = {
            "ok": True,
            "tag": tag,
            "showing": True,
            "bounds": {"x": x, "y": y, "width": w, "height": h},
            "center": {"x": x + w // 2, "y": y + h // 2},
            "coordinate_space": "logical",
        }
        if layer:
            result["layer"] = layer
        screen_width = getattr(renpy.config, "screen_width", None)
        screen_height = getattr(renpy.config, "screen_height", None)
        if screen_width and screen_height:
            result["screen"] = {"width": int(screen_width), "height": int(screen_height)}
        return result

    _RENFORGE_POSITION_FIELDS = (
        "xpos", "ypos", "xanchor", "yanchor",
        "xalign", "yalign", "xoffset", "yoffset",
        "zoom", "rotate",
    )

    def _renforge_h_show_displayable(payload):
        # Reposition an already-showing image tag at runtime and return where it
        # landed. This turns "edit .rpy, relaunch, look, guess the offset" into
        # an interactive loop: converge on live coordinates, then write the
        # final values into the script. The tag keeps its current attributes,
        # so `show eileen happy` stays happy after a nudge.
        payload = payload or {}
        tag = payload.get("tag")
        if not tag:
            return {"ok": False, "error": "position_element requires a tag"}
        tag = str(tag)
        layer = payload.get("layer")
        layer = str(layer) if layer else None

        transform_kwargs = {}
        for field in _RENFORGE_POSITION_FIELDS:
            value = payload.get(field)
            if value is None:
                continue
            # Preserve int vs float: Ren'Py reads an int position as absolute
            # pixels and a float as a fraction of the screen (xpos 600 == 600px,
            # xpos 0.5 == halfway). Coercing to float would turn "600 pixels"
            # into 600x the screen width.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return {"ok": False, "error": "%s must be a number" % field}
            transform_kwargs[field] = value
        if not transform_kwargs:
            return {"ok": False, "error": "position_element requires at least one placement field"}

        try:
            showing = list(renpy.get_showing_tags(layer)) if layer else list(renpy.get_showing_tags())
        except Exception:
            showing = []
        if tag not in showing:
            return {
                "ok": False,
                "error": "tag %r is not showing; show it first" % tag,
                "tag": tag,
                "showing_tags": showing,
            }

        transform_cls = getattr(renpy.store, "Transform", None)
        if transform_cls is None:
            return {"ok": False, "error": "Transform is unavailable in the store"}
        show = getattr(renpy, "show", None)
        if not callable(show):
            return {"ok": False, "error": "renpy.show is unavailable"}
        try:
            transform = transform_cls(**transform_kwargs)
            if layer:
                show(tag, at_list=[transform], layer=layer)
            else:
                show(tag, at_list=[transform])
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

        # get_image_bounds reads the last drawn frame; force a render so the
        # reported bounds reflect the show we just applied rather than the
        # previous position.
        restart = getattr(renpy, "restart_interaction", None)
        if callable(restart):
            try:
                restart()
            except Exception:
                pass
        try:
            renpy.screenshot_to_bytes(None)
        except Exception:
            pass

        result = _renforge_h_get_displayable_bounds({"tag": tag, "layer": layer})
        result["applied"] = transform_kwargs
        return result

    def _renforge_h_select_choice(payload):
        """Choose one listed control with text, by ``text`` or by ``index``.

        Text matches exactly (ignoring case) first, then as a substring; an
        index counts the controls that have text. More than one match is
        refused as ambiguous, and a disabled or covered control is refused.
        When the caller sends interaction/session/observation ids, a stale
        observation is refused. A semantic control (a menu item, a known
        action) runs its action; anything else is clicked at its center.
        """
        payload = payload or {}
        raw, pairs = _renforge_observe_raw(False, with_pairs=True, include_readout=False)
        contract = _renforge_runtime.interaction
        if any(payload.get(key) is not None for key in ("interaction", "session_id", "observation_id")):
            refusal = contract.guard_identity(raw, payload.get("interaction", raw["interaction"]),
                session_id=payload.get("session_id"), observation_id=payload.get("observation_id"))
            if refusal:
                return {"ok": False, "error": refusal, "observation": raw}
        choices = [(focus, record) for focus, record in pairs if record.get("text")]
        matches = []
        text, index = payload.get("text"), payload.get("index")
        if text is not None:
            needle = str(text).casefold()
            matches = [(focus, record) for focus, record in choices if record["text"].casefold() == needle]
            if not matches:
                matches = [(focus, record) for focus, record in choices if needle in record["text"].casefold()]
        elif isinstance(index, builtins.int) and not isinstance(index, bool) and 0 <= index < len(choices):
            matches = [choices[index]]
        if not matches:
            return {"ok": False, "error": "no choice matching %r/%r" % (text, index)}
        if len(matches) != 1:
            return {"ok": False, "error": "ambiguous choice; provide its listed index", "matches": [record["text"] for _focus, record in matches]}
        focus, record = matches[0]
        if not record.get("enabled", True):
            return {"ok": False, "error": "disabled"}
        if record.get("covered") or record.get("clickable") is False:
            return {"ok": False, "error": "covered"}
        result = {"ok": True, "text": record["text"], "id": record["id"], "interaction": raw["interaction"]}
        try:
            if record.get("semantic"):
                refusal = contract.guard_identity(raw, raw["interaction"])
                if refusal:
                    return {"ok": False, "error": refusal, "observation": raw}
                if not _renforge_invoke_focus_action(focus):
                    return {"ok": False, "error": "unsupported"}
                result["via"] = "action"
            else:
                x, y = _renforge_click_focus(focus, record)
                result.update(x=x, y=y, via="pointer")
        except Exception as exc:
            if not _renforge_is_script_control_flow(exc):
                raise
            result["via"] = "action" if record.get("semantic") else "pointer"
            pointer = getattr(exc, "renforge_pointer", None)
            if pointer:
                result.update(x=pointer[0], y=pointer[1])
            setattr(exc, "renforge_result", result)
            raise
        return result

    # Scene inspection shares control discovery and completed render geometry.
    _RENFORGE_SCENE_MAX_NODES = 4000
    # observe: controls listed (and menu entries read), scene nodes walked,
    # and custom event handlers reported as input candidates.
    _RENFORGE_OBSERVE_MAX_CONTROLS = 400
    _RENFORGE_OBSERVE_MAX_NODES = 5000
    _RENFORGE_OBSERVE_MAX_CANDIDATES = 64
    _RENFORGE_SCENE_MAX_DEPTH = 48
    _RENFORGE_SCENE_MAX_TEXT_CHARS = 4096
    _RENFORGE_SCENE_MAX_ID_CHARS = 256
    _RENFORGE_SCENE_SEMANTIC_TYPES = ("image", "text", "button", "bar", "input", "imagemap", "hotspot")
    # Only these node types can wrap a text displayable worth measuring.
    _RENFORGE_SCENE_OVERFLOW_TYPES = ("text", "button", "input")
    _RENFORGE_SCENE_OVERFLOW_SEARCH_DEPTH = 4
    _RENFORGE_SCENE_OVERFLOW_SEARCH_LIMIT = 16
    _RENFORGE_SCENE_OVERFLOW_BASIS = (
        "unwrapped text width and rendered wrapped height vs effective pixel maximum"
    )
    def _renforge_scene_string(value, max_chars):
        if value is None:
            return None
        try:
            text = str(value)
        except Exception:
            return None
        return text[:max_chars] + ("…" if len(text) > max_chars else "")



    def _renforge_scene_window():
        w = getattr(renpy.config, "screen_width", 0) or 0
        h = getattr(renpy.config, "screen_height", 0) or 0
        return int(w), int(h)

    def _renforge_scene_type(d):
        try:
            if isinstance(d, renpy.text.text.Text):
                return "text"
        except Exception:
            pass
        name = getattr(getattr(d, "__class__", None), "__name__", "") or ""
        lowered = name.casefold()
        for marker, node_type in (
            ("imagebutton", "button"), ("textbutton", "button"), ("button", "button"),
            ("imagemap", "imagemap"), ("bar", "bar"), ("input", "input"),
            ("text", "text"), ("frame", "container"), ("window", "container"),
            ("viewport", "container"), ("vbox", "container"), ("hbox", "container"),
            ("fixed", "container"), ("grid", "container"), ("side", "container"),
            ("multibox", "container"), ("screen", "container"),
            ("solid", "image"), ("image", "image"), ("transform", "image"),
        ):
            if marker in lowered:
                return node_type
        return name or "other"

    def _renforge_scene_text(d, max_chars):
        return _renforge_scene_string(_renforge_observe_text_string(d, max_chars=None), max_chars)

    def _renforge_scene_node(node_id, node_type, layer, screen, geometry, text=None, action=None, enabled=None):
        node = {
            "id": node_id,
            "type": node_type,
            "layer": layer,
            "screen": screen,
            "visible": True,
            "coordinate_space": "logical",
        }
        node.update(geometry)
        bounds = geometry["bounds"]
        node["bounds_available"] = bounds is not None
        node["center"] = _renforge_bounds_center(bounds)
        if text is not None:
            node["text"] = text
        if action is not None:
            node["action"] = action
        if enabled is not None:
            node["enabled"] = enabled
        return node

    def _renforge_color_hex(color):
        try:
            if isinstance(color, str):
                return color
            if isinstance(color, (tuple, list)) and len(color) >= 3:
                return "#%02X%02X%02X" % (int(color[0]), int(color[1]), int(color[2]))
        except Exception:
            pass
        return str(color)[:24]

    def _renforge_scene_style(d):
        style = getattr(d, "style", None)
        if style is None:
            return None
        out = {}
        color = getattr(style, "color", None)
        if color is not None:
            out["color"] = _renforge_color_hex(color)
        size = getattr(style, "size", None)
        if isinstance(size, int):
            out["size"] = size
        font = getattr(style, "font", None)
        if font:
            out["font"] = str(font)[:60]
        bg = getattr(style, "background", None)
        if bg is not None:
            out["background"] = str(bg)[:60]
        for attr_name in ("xmaximum", "ymaximum"):
            value = getattr(style, attr_name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                out[attr_name] = value
        return out or None

    def _renforge_scene_text_subject(d, depth=0, seen=None):
        # Buttons and inputs wrap the displayable that actually draws text.
        # Search a few levels down, without following cycles. The first text
        # child is the subject; controls normally expose one label, and picking
        # a "largest" child would guess at intent.
        if d is None:
            return None
        if seen is None:
            seen = set()
        if id(d) in seen:
            return None
        seen.add(id(d))
        if _renforge_scene_type(d) == "text":
            return d
        if depth >= _RENFORGE_SCENE_OVERFLOW_SEARCH_DEPTH:
            return None
        candidates = []
        try:
            children = getattr(d, "children", None)
        except Exception:
            children = None
        if children:
            try:
                for index, child in enumerate(children):
                    if index >= _RENFORGE_SCENE_OVERFLOW_SEARCH_LIMIT:
                        break
                    candidates.append(child)
            except Exception:
                pass
        else:
            try:
                child = getattr(d, "child", None)
            except Exception:
                child = None
            if child is not None:
                candidates.append(child)
        get_child = getattr(d, "get_child", None)
        if callable(get_child):
            try:
                child = get_child()
            except Exception:
                child = None
            if child is not None:
                candidates.append(child)
        for child in candidates:
            subject = _renforge_scene_text_subject(child, depth + 1, seen)
            if subject is not None:
                return subject
        return None

    def _renforge_scene_text_size(subject, width, height):
        try:
            size_fn = getattr(subject, "size", None)
            if callable(size_fn):
                measured = size_fn(width, height, 0, 0)
            else:
                render_for_size = getattr(getattr(renpy.display, "render", None), "render_for_size", None)
                if not callable(render_for_size):
                    return None, "no size or render API"
                surf = render_for_size(subject, width, height, 0, 0)
                measured = (getattr(surf, "width", None), getattr(surf, "height", None))
            return {"width": int(measured[0]), "height": int(measured[1])}, None
        except Exception as exc:
            return None, "size measurement failed: %s" % exc

    def _renforge_scene_pixel_maximum(value):
        # Ren'Py treats an int maximum as a pixel ceiling and a float maximum
        # as a fraction of the parent's size. Only the int form is comparable.
        if isinstance(value, int) and not isinstance(value, bool):
            return int(value)
        return None

    def _renforge_scene_constraint_value(value):
        if value is None or isinstance(value, (bool, int, float)):
            return value
        try:
            return str(value)[:40]
        except Exception:
            return None

    def _renforge_scene_style_value(style, attr_name):
        if style is None:
            return None
        try:
            return getattr(style, attr_name, None)
        except Exception:
            return None

    def _renforge_scene_spacing(style, suffix):
        # Padding and margins only narrow the inner box when declared in
        # pixels; relative or unknown values are skipped rather than guessed.
        spacing = {}
        for side in ("left", "right", "top", "bottom"):
            value = _renforge_scene_style_value(style, side + "_" + suffix)
            if isinstance(value, int) and not isinstance(value, bool):
                spacing[side] = value
        return spacing

    def _renforge_scene_overflow_unavailable(reason, natural=None, constraints=None):
        verdict = {
            "available": False,
            "reason": reason,
            "overflow": None,
            "overflow_px": None,
        }
        if natural is not None:
            verdict["natural"] = natural
        if constraints is not None:
            verdict["constraints"] = constraints
        return verdict

    def _renforge_scene_overflow(d):
        subject = _renforge_scene_text_subject(d)
        if subject is None:
            return _renforge_scene_overflow_unavailable("no text subject")
        subject_style = getattr(subject, "style", None)
        if _renforge_scene_style_value(subject_style, "vertical") is True:
            # Text.size lays vertical text out horizontally, so a verdict
            # built from it would report the overflow on the wrong axis.
            return _renforge_scene_overflow_unavailable("vertical text is not measured")
        host_style = getattr(d, "style", None)
        host_is_subject = d is subject
        natural, error = _renforge_scene_text_size(subject, 1000000, 1000000)
        if natural is None:
            return _renforge_scene_overflow_unavailable(error)
        constraints = {}
        maximum_source = {}
        padding = _renforge_scene_spacing(host_style, "padding")
        margin = _renforge_scene_spacing(host_style, "margin")
        effective = {}
        for axis, attr_name, sides in (
            ("x", "xmaximum", ("left", "right")),
            ("y", "ymaximum", ("top", "bottom")),
        ):
            host_value = _renforge_scene_style_value(host_style, attr_name)
            subject_value = _renforge_scene_style_value(subject_style, attr_name)
            constraints["host_" + attr_name] = _renforge_scene_constraint_value(host_value)
            constraints["subject_" + attr_name] = _renforge_scene_constraint_value(subject_value)
            host_ceiling = _renforge_scene_pixel_maximum(host_value)
            if host_ceiling is not None and not host_is_subject:
                # Only the host box carries padding and margins, and they
                # narrow its inner box. The subject text maximum is already
                # expressed in the text's own layout, so it is used as written.
                for side in sides:
                    host_ceiling -= padding.get(side, 0) + margin.get(side, 0)
                host_ceiling = max(0, host_ceiling)
            # Ren'Py applies the host and the subject maximums together, so the
            # effective ceiling is the tighter of the two. When the host is the
            # subject both readings collapse into one and the same constraint.
            candidates = []
            if host_ceiling is not None:
                candidates.append(("host", host_ceiling))
            if not host_is_subject:
                subject_ceiling = _renforge_scene_pixel_maximum(subject_value)
                if subject_ceiling is not None:
                    candidates.append(("subject", subject_ceiling))
            ceiling = None
            for _, candidate in candidates:
                ceiling = candidate if ceiling is None else min(ceiling, candidate)
            if not candidates:
                source = None
            elif len(candidates) == 1:
                source = candidates[0][0]
            else:
                source = "both"
            effective[axis] = ceiling
            maximum_source[axis] = source
        constraints["effective_xmaximum"] = effective["x"]
        constraints["effective_ymaximum"] = effective["y"]
        constraints["maximum_source"] = maximum_source
        if not host_is_subject:
            constraints["padding"] = padding
            constraints["margin"] = margin
        if effective["x"] is None and effective["y"] is None:
            return _renforge_scene_overflow_unavailable(
                "no absolute maximum declared", natural, constraints
            )
        if effective["x"] is None:
            # Without an absolute width the real wrap width is unknown; the
            # unwrapped height cannot prove that the text fits vertically.
            return _renforge_scene_overflow_unavailable(
                "no absolute x maximum; wrapped height unknown", natural, constraints
            )
        rendered, error = _renforge_scene_text_size(subject, effective["x"], 1000000)
        if rendered is None:
            return _renforge_scene_overflow_unavailable(error, natural, constraints)
        axes = {"x": max(0, natural["width"] - effective["x"]), "y": None}
        if effective["y"] is not None:
            axes["y"] = max(0, rendered["height"] - effective["y"])
        # x records how far the text must wrap; y records actual vertical
        # clipping. A paragraph that wraps but still fits is not an overflow.
        overflow = None if axes["y"] is None else axes["y"] > 0
        return {
            "available": True,
            "complete": effective["y"] is not None,
            "unmeasured_axes": [] if effective["y"] is not None else ["y"],
            "overflow": overflow,
            "overflow_axes": axes,
            "overflow_px": axes["y"],
            "wrapped": {"x": axes["x"] > 0, "y": overflow is True},
            "natural": natural,
            "rendered": rendered,
            "constraints": constraints,
            "basis": _RENFORGE_SCENE_OVERFLOW_BASIS,
        }

    def _renforge_scene_finalize(nodes, include):
        want_style = "style" in include
        want_overflow = "overflow" in include
        for node in nodes:
            d = node.pop("_d", None)
            if d is None:
                continue
            if want_style:
                style = _renforge_scene_style(d)
                if style:
                    node["style"] = style
            if want_overflow and node.get("type") in _RENFORGE_SCENE_OVERFLOW_TYPES:
                node["overflow"] = _renforge_scene_overflow(d)

    def _renforge_scene_descend_children(d, layer, screen, zorder, seen, out, counters, depth):
        children = getattr(d, "children", None)
        if not children:
            child = getattr(d, "child", None)
            if child is None:
                return
            if depth >= counters["_max_depth"]:
                counters["_omitted_max_depth"] += 1
                return
            _renforge_scene_descend(child, layer, screen, zorder, seen, out, counters, depth + 1)
            return
        if depth >= counters["_max_depth"]:
            counters["_omitted_max_depth"] += len(children)
            return
        for child in children:
            if len(out) >= counters["_max_nodes"]:
                counters["_omitted_max_nodes"] += 1
                break
            _renforge_scene_descend(child, layer, screen, zorder, seen, out, counters, depth + 1)

    def _renforge_scene_descend(d, layer, screen, zorder, seen, out, counters, depth):
        if len(out) >= counters["_max_nodes"]:
            counters["_omitted_max_nodes"] += 1
            return
        if d is None or id(d) in seen:
            return
        seen.add(id(d))
        node_type = _renforge_scene_type(d)
        key = (layer, screen or "", node_type)
        ordinal = counters.get(key, 0)
        counters[key] = ordinal + 1
        screen_id = _renforge_scene_string(screen, _RENFORGE_SCENE_MAX_ID_CHARS)
        if screen_id:
            node_id = "%s/%s.%s#%d" % (layer, screen_id, node_type, ordinal)
        else:
            node_id = "%s/%s#%d" % (layer, node_type, ordinal)
        type_filter = counters["_flt_types"]
        eligible = (
            (node_type.casefold() in type_filter)
            if type_filter is not None
            else _renforge_scene_detail_ok(node_type, counters["_detail"])
        )
        eligible = eligible and (
            counters["_flt_layers"] is None or layer in counters["_flt_layers"]
        )
        text = (_renforge_scene_text(d, counters["_max_text_chars"])
                if eligible and node_type == "text" else None)
        geometry = _renforge_widget_geometry(d, counters["_inventory"])
        node = _renforge_scene_node(node_id, node_type, layer, screen, geometry, text=text)
        node["zorder"] = zorder
        node["occurrences"] = counters["_inventory"]["geometry"]["occurrences"].get(id(d), [])
        node["_d"] = d
        out.append(node)
        _renforge_scene_descend_children(d, layer, screen, zorder, seen, out, counters, depth)

    def _renforge_scene_unique_ids(nodes, max_text_chars):
        used = set()
        for node in nodes:
            node.setdefault("_raw_type", node.get("type"))
            node.setdefault("_raw_layer", node.get("layer"))
            node.setdefault("_raw_screen", node.get("screen"))
            for key in ("text", "screen", "action", "tag", "layer", "type"):
                if node.get(key) is not None:
                    node[key] = _renforge_scene_string(node[key], max_text_chars)
            base = _renforge_scene_string(
                node.get("id") or "node",
                _RENFORGE_SCENE_MAX_ID_CHARS,
            )
            candidate = base
            suffix_number = 2
            while candidate in used:
                suffix = "#%d" % suffix_number
                candidate = base[:max(1, _RENFORGE_SCENE_MAX_ID_CHARS - len(suffix))] + suffix
                suffix_number += 1
            node["id"] = candidate
            used.add(candidate)


    def _renforge_scene_detail_ok(node_type, detail):
        if detail == "raw":
            return True
        if detail == "layout":
            return node_type in _RENFORGE_SCENE_SEMANTIC_TYPES or node_type == "container"
        return node_type in _RENFORGE_SCENE_SEMANTIC_TYPES

    def _renforge_scene_limit(payload, name, default, minimum):
        try:
            requested = int(payload.get(name, default))
        except Exception:
            requested = default
        return min(default, max(minimum, requested))


    def _renforge_h_scene_tree(payload):
        """The scene as nodes: controls first, then each layer's displayables.

        Filters (layers, types, screen, ids), detail level and the max_depth,
        max_nodes and max_text_chars limits come from the payload; what the
        limits cut is counted under ``omitted``.
        """
        payload = payload or {}
        detail = str(payload.get("detail") or "semantic").casefold()
        if detail not in ("semantic", "layout", "raw"):
            detail = "semantic"
        flt_layers = payload.get("layers")
        flt_layers = set(str(x) for x in flt_layers) if flt_layers else None
        flt_types = payload.get("types")
        flt_types = set(str(x).casefold() for x in flt_types) if flt_types else None
        flt_screen = payload.get("screen")
        flt_screen = str(flt_screen) if flt_screen else None
        flt_ids = payload.get("ids")
        flt_ids = set(str(x) for x in flt_ids) if flt_ids else None
        include = payload.get("include")
        include = set(str(x).casefold() for x in include) if include else set()

        max_depth = _renforge_scene_limit(payload, "max_depth", _RENFORGE_SCENE_MAX_DEPTH, 0)
        max_nodes = _renforge_scene_limit(payload, "max_nodes", _RENFORGE_SCENE_MAX_NODES, 1)
        max_text_chars = _renforge_scene_limit(
            payload, "max_text_chars", _RENFORGE_SCENE_MAX_TEXT_CHARS, 16
        )
        width, height = _renforge_scene_window()
        seen = set()
        nodes = []
        counters = {
            "_max_depth": max_depth,
            "_max_nodes": max_nodes,
            "_max_text_chars": max_text_chars,
            "_detail": detail,
            "_flt_layers": flt_layers,
            "_flt_types": flt_types,
            "_flt_screen": flt_screen,
            "_flt_ids": flt_ids,
            "_omitted_max_depth": 0,
            "_omitted_max_nodes": 0,
        }

        inventory = _renforge_scene_inventory()
        counters["_inventory"] = inventory
        # Controls and other nodes share the same completed frame geometry.
        # Input candidates are judged against every control, not only the
        # ones max_nodes leaves room to list.
        try:
            controls = _renforge_focusable_elements(None, max_text_chars, inventory)
        except Exception:
            controls = []
        focusables = controls[:max_nodes + 1]
        if len(focusables) > max_nodes:
            counters["_omitted_max_nodes"] += len(focusables) - max_nodes
            for _focus, element in focusables[:max_nodes]:
                element["covered"] = None
                element["clickable"] = None
                element["coverage_reason"] = "max_nodes"
            focusables = focusables[:max_nodes]
        for focus, element in focusables:
            if len(nodes) >= max_nodes:
                counters["_omitted_max_nodes"] += len(focusables) - len(nodes)
                break
            widget = getattr(focus, "widget", None)
            if widget is not None:
                seen.add(id(widget))
            node = dict(element)
            node.setdefault("layer", "screens")
            node["bounds_available"] = node.get("bounds") is not None
            node.pop("index", None)
            node.pop("role", None)
            node["_d"] = widget
            nodes.append(node)

        # Every layer's top-level displayables, with real logical bounds.
        try:
            sl = renpy.game.context().scene_lists
        except Exception:
            sl = None
        if sl is not None:
            layer_map = getattr(sl, "layers", {}) or {}
            try:
                ordered = list(getattr(renpy.display.scenelists, "ordered_layers", None) or renpy.config.layers)
            except Exception:
                ordered = list(layer_map.keys())
            layer_names = [l for l in ordered if l in layer_map] + [l for l in layer_map if l not in ordered]
            layer_counts = {}
            limit_hit = False
            for layer in layer_names:
                if limit_hit:
                    break
                for sle in layer_map.get(layer, []):
                    if len(nodes) >= max_nodes:
                        counters["_omitted_max_nodes"] += 1
                        limit_hit = True
                        break
                    d = getattr(sle, "displayable", None)
                    if d is None or id(d) in seen:
                        continue
                    seen.add(id(d))
                    geometry = _renforge_widget_geometry(d, inventory)
                    node_type = _renforge_scene_type(d)
                    tag = getattr(sle, "tag", None)
                    tag_text = _renforge_scene_string(tag, max_text_chars)
                    tag_id = _renforge_scene_string(tag, _RENFORGE_SCENE_MAX_ID_CHARS)
                    screen_name = None
                    sn = getattr(d, "screen_name", None)
                    if isinstance(sn, (tuple, list)) and sn:
                        screen_name = str(sn[0])
                    elif sn:
                        screen_name = str(sn)
                    elif layer == "screens" and tag:
                        screen_name = str(tag)
                    if tag:
                        node_id = "%s/%s" % (layer, tag_id)
                    else:
                        idx = layer_counts.get((layer, node_type), 0)
                        layer_counts[(layer, node_type)] = idx + 1
                        node_id = "%s/%s#%d" % (layer, node_type, idx)
                    text = (
                        _renforge_scene_text(d, max_text_chars)
                        if node_type == "text"
                        else None
                    )
                    node = _renforge_scene_node(node_id, node_type, layer, screen_name, geometry, text=text)
                    node["zorder"] = int(getattr(sle, "zorder", 0) or 0)
                    if tag:
                        node["tag"] = tag_text
                    node["occurrences"] = inventory["geometry"]["occurrences"].get(id(d), [])
                    node["_d"] = d
                    nodes.append(node)
                    _renforge_scene_descend_children(d, layer, screen_name, node["zorder"], seen, nodes, counters, 0)

        input_candidates, _unclassified = _renforge_input_candidates(inventory, controls)
        _renforge_scene_unique_ids(nodes, max_text_chars)
        _renforge_scene_finalize(nodes, include)

        perceived_by_type = {}
        perceived_by_layer = {}
        for n in nodes:
            perceived_by_type[n["type"]] = perceived_by_type.get(n["type"], 0) + 1
            lk = n.get("layer") or "?"
            perceived_by_layer[lk] = perceived_by_layer.get(lk, 0) + 1

        returned = []
        for n in nodes:
            raw_type = n.get("_raw_type")
            raw_layer = n.get("_raw_layer")
            raw_screen = n.get("_raw_screen")
            if flt_types is None and not _renforge_scene_detail_ok(raw_type, detail):
                continue
            if flt_layers is not None and raw_layer not in flt_layers:
                continue
            if flt_types is not None and str(raw_type).casefold() not in flt_types:
                continue
            if flt_screen is not None and raw_screen != flt_screen:
                continue
            if flt_ids is not None and (str(n.get("id")) not in flt_ids):
                continue
            returned.append(n)

        ret_by_type = {}
        ret_by_layer = {}
        for n in returned:
            ret_by_type[n["type"]] = ret_by_type.get(n["type"], 0) + 1
            lk = n.get("layer") or "?"
            ret_by_layer[lk] = ret_by_layer.get(lk, 0) + 1
        omit_by_type = {t: perceived_by_type[t] - ret_by_type.get(t, 0)
                        for t in perceived_by_type if perceived_by_type[t] - ret_by_type.get(t, 0) > 0}
        omit_by_layer = {l: perceived_by_layer[l] - ret_by_layer.get(l, 0)
                         for l in perceived_by_layer if perceived_by_layer[l] - ret_by_layer.get(l, 0) > 0}
        for n in nodes:
            n.pop("_raw_type", None)
            n.pop("_raw_layer", None)
            n.pop("_raw_screen", None)
        omit_by_reason = {}
        if counters["_omitted_max_depth"]:
            omit_by_reason["max_depth"] = counters["_omitted_max_depth"]
        if counters["_omitted_max_nodes"]:
            omit_by_reason["max_nodes"] = counters["_omitted_max_nodes"]

        return {
            "ok": True,
            "coordinate_space": "logical",
            "window": {"width": width, "height": height},
            "detail": detail,
            "nodes": returned,
            "input_candidates": input_candidates,
            "counts": {"perceived": len(nodes), "returned": len(returned)},
            "omitted": {
                "by_type": omit_by_type,
                "by_layer": omit_by_layer,
                "by_reason": omit_by_reason,
            },
            "truncated": bool(omit_by_reason),
            "limits": {
                "max_depth": max_depth,
                "max_nodes": max_nodes,
                "max_text_chars": max_text_chars,
            },
        }

    _RENFORGE_ENGINE_WIDGETS = (("dragdrop", "Drag", "drag"), ("viewport", "Viewport", "viewport"),
                                ("behavior", "Input", "input"), ("behavior", "Bar", "bar"),
                                ("behavior", "Button", "button"))

    def _renforge_engine_role(widget):
        """The role Ren'Py's own widget classes give, subclasses included."""
        display = getattr(renpy, "display", None)
        for module, name, role in _RENFORGE_ENGINE_WIDGETS:
            cls = getattr(getattr(display, module, None), name, None)
            if isinstance(cls, type) and isinstance(widget, cls):
                return role
        return None

    def _renforge_is_engine_widget(widget, name):
        """Whether ``widget`` is Ren'Py's ``name`` widget (Drag, Viewport, Input,
        Bar, Button), a subclass such as VPGrid included.

        A widget whose class has exactly that name also counts, as before, for
        runtimes where the engine class cannot be resolved.
        """
        if widget is None:
            return False
        module = next((module for module, known, _role in _RENFORGE_ENGINE_WIDGETS if known == name), None)
        cls = getattr(getattr(getattr(renpy, "display", None), module, None), name, None) if module else None
        try:
            if isinstance(cls, type) and isinstance(widget, cls):
                return True
            return widget.__class__.__name__ == name
        except Exception:
            return False

    def _renforge_observe_role(focus, widget):
        # Ren'Py's classes first; names only for displayables that are not
        # the engine's. The shared contract normalizes the role.
        engine_role = _renforge_engine_role(widget) if widget is not None else None
        if engine_role is not None:
            return engine_role
        class_name = ""
        try:
            if widget is not None:
                class_name = widget.__class__.__name__.casefold()
        except Exception:
            class_name = ""
        if "hotspot" in class_name:
            return "hotspot"
        if "imagebutton" in class_name:
            return "imagebutton"
        if "textbutton" in class_name:
            return "textbutton"
        if "slider" in class_name:
            return "slider"
        if "viewport" in class_name:
            return "viewport"
        if class_name.startswith("drag"):
            return "drag"
        if class_name == "input" or (class_name.endswith("input") and "button" not in class_name):
            return "input"
        if class_name == "bar" or class_name.endswith("bar"):
            return "bar"
        return _renforge_focus_type(focus, widget)


    def _renforge_observe_normalize_role(role):
        return _renforge_runtime.interaction.normalize_role(role)

    def _renforge_observe_operations(role):
        return _renforge_runtime.interaction.operations_for(role)

    def _renforge_observe_is_sequence(value):
        # `init python` globals bind `list` to Ren'Py's RevertableList. Engine
        # objects such as SayBehavior.dismiss and Transform.children are
        # ordinary lists, and isinstance(..., list) misses them.
        return isinstance(value, (builtins.list, builtins.tuple))


    def _renforge_observe_event_names(value):
        if isinstance(value, builtins.str):
            return [value]
        if value is None or isinstance(value, (builtins.dict, builtins.int, bool)):
            return []
        try:
            items = iter(value)
        except TypeError:
            return []
        return [item for item in items if isinstance(item, builtins.str)]


    def _renforge_observe_basename(image_name):
        return _renforge_runtime.interaction.image_basename(image_name)

    def _renforge_displayable_filename(node, depth=0):
        if node is None or depth > 6:
            return None
        filename = getattr(node, "filename", None)
        if isinstance(filename, str) and filename.strip():
            return filename.strip()
        # A DynamicImage's name is its template ("[outfit]_portrait");
        # the image it resolved to is what is drawn.
        if node.__class__.__name__ == "DynamicImage":
            for attr in ("raw_target", "target"):
                resolved = getattr(node, attr, None)
                if resolved is not None and resolved is not node:
                    found = _renforge_displayable_filename(resolved, depth + 1)
                    if found:
                        return found
        name = getattr(node, "name", None)
        if isinstance(name, str) and name.strip() and name not in (
            "image",
            "Image",
            "Transform",
            "default",
        ):
            return name.strip()
        if _renforge_observe_is_sequence(name):
            parts = [part for part in name if isinstance(part, str) and part.strip()]
            if parts:
                # An image name is a tag and attributes ("eileen happy"),
                # written as Ren'Py writes it, not a path.
                return " ".join(parts)
        child = getattr(node, "child", None)
        if child is not None and child is not node:
            found = _renforge_displayable_filename(child, depth + 1)
            if found:
                return found
        children = getattr(node, "children", None)
        if _renforge_observe_is_sequence(children):
            for child in children:
                found = _renforge_displayable_filename(child, depth + 1)
                if found:
                    return found
        return None


    def _renforge_observe_image_name(widget):
        if widget is None:
            return None
        try:
            class_name = widget.__class__.__name__.casefold()
        except Exception:
            return None
        if "imagebutton" not in class_name and "hotspot" not in class_name:
            return None
        state = getattr(widget, "state_children", None)
        idle = None
        if isinstance(state, builtins.dict):
            idle = state.get("idle_")
            if idle is None:
                idle = state.get("idle")
        if idle is None:
            return None
        return _renforge_displayable_filename(idle)


    def _renforge_observe_widget_id(focus, widget, named):
        for owner in (widget, focus):
            value = getattr(owner, "mcp_id", None)
            if isinstance(value, builtins.str) and value.strip():
                return value.strip()
        candidates = (widget, getattr(widget, "child", None))
        for candidate in candidates:
            if candidate is not None and id(candidate) in named:
                return named[id(candidate)]
        # A focus owner may not be in the scene walk (for example during a
        # screen rebuild). Use that instance, never a default-layer name lookup.
        widgets = getattr(getattr(focus, "screen", None), "widgets", None)
        if isinstance(widgets, builtins.dict):
            for index, (name, candidate) in enumerate(widgets.items()):
                if index >= _RENFORGE_OBSERVE_MAX_CONTROLS:
                    break
                if (isinstance(name, builtins.str) and name.strip()
                        and candidate is not None and any(candidate is item for item in candidates)):
                    return name.strip()
        return None


    # Depth 3 still formats a list of If whose true branch is a list of actions.
    # 200 characters keeps a URL or location that differs at the end.
    _RENFORGE_OBSERVE_DEPTH = 3
    _RENFORGE_OBSERVE_QUOTE_LIMIT = 200
    _RENFORGE_READOUT_LIMIT = _renforge_runtime.interaction.READOUT_LIMIT
    _RENFORGE_READOUT_TEXT_LIMIT = _renforge_runtime.interaction.READOUT_TEXT_LIMIT


    def _renforge_observe_quote(value, allow_bool=False):
        if isinstance(value, bool):
            if not allow_bool:
                return None
            return "True" if value else "False"
        if isinstance(value, builtins.int):
            return str(value)
        if not isinstance(value, builtins.str):
            return None
        text = value.strip()
        if not text:
            return None
        text = text.replace("\\", "\\\\").replace('"', '\\"')
        if len(text) > _RENFORGE_OBSERVE_QUOTE_LIMIT:
            text = text[: _RENFORGE_OBSERVE_QUOTE_LIMIT - 3] + "..."
        return '"%s"' % text


    def _renforge_observe_callable_name(callable_value):
        if callable_value is None:
            return None
        name = getattr(callable_value, "__name__", None)
        if isinstance(name, builtins.str):
            cleaned = name.strip()
            if cleaned and cleaned.replace("_", "a").isalnum() and not cleaned[0].isdigit():
                return cleaned
        try:
            class_name = callable_value.__class__.__name__
        except Exception:
            return None
        if isinstance(class_name, builtins.str) and class_name and class_name not in (
            "type",
            "builtin_function_or_method",
            "function",
            "method",
        ):
            return class_name
        return None


    def _renforge_observe_object_token(value):
        # An object argument is named by its fileName field, else its name.
        for attr in ("fileName", "name"):
            try:
                field = getattr(value, attr)
            except Exception:
                continue
            quoted = _renforge_observe_quote(field)
            if quoted:
                return quoted
        return None


    def _renforge_observe_format_function(value, depth):
        parts = []
        callable_name = _renforge_observe_callable_name(getattr(value, "callable", None))
        if callable_name:
            parts.append(callable_name)
        args = getattr(value, "args", ())
        if _renforge_observe_is_sequence(args):
            for arg in args:
                if isinstance(arg, bool):
                    parts.append("True" if arg else "False")
                    continue
                quoted = _renforge_observe_quote(arg)
                if quoted:
                    parts.append(quoted)
                    continue
                token = _renforge_observe_object_token(arg)
                if token:
                    parts.append(token)
        kwargs = getattr(value, "kwargs", None)
        if isinstance(kwargs, builtins.dict):
            names = []
            for key in kwargs:
                if isinstance(key, builtins.str):
                    names.append(key)
            for key in sorted(names):
                item = kwargs[key]
                if isinstance(item, bool):
                    parts.append("%s=%s" % (key, "True" if item else "False"))
                    continue
                quoted = _renforge_observe_quote(item)
                if quoted:
                    parts.append("%s=%s" % (key, quoted))
                    continue
                token = _renforge_observe_object_token(item)
                if token:
                    parts.append("%s=%s" % (key, token))
        if not parts:
            return "Function"
        return "Function(%s)" % ", ".join(parts)


    _RENFORGE_MANGLED_NAME = re.compile(r"^_m\d+_\w*?__([A-Za-z]\w*)$")


    def _renforge_observe_format_action(value, depth=0):
        # Class name plus the field that tells two buttons apart: ShowMenu("load")
        # versus ShowMenu("preferences"), Start("start"), Jump("chapter_two").
        if value is None or depth > _RENFORGE_OBSERVE_DEPTH:
            return None
        if _renforge_observe_is_sequence(value):
            labels = []
            for item in value:
                label = _renforge_observe_format_action(item, depth + 1)
                if label:
                    labels.append(label)
            if not labels:
                return None
            if len(labels) == 1:
                return labels[0]
            return "; ".join(labels)
        try:
            name = value.__class__.__name__
        except Exception:
            return None
        if not name or name in (
            "list",
            "tuple",
            "dict",
            "object",
            "NoneType",
            "RevertableList",
        ):
            return None
        # Ren'Py's If() returns the chosen action immediately. An object that
        # still holds both branches contributes the true one.
        if name in ("If", "SensitiveIf", "SelectedIf"):
            true_action = getattr(value, "true", None)
            if true_action is None:
                true_action = getattr(value, "expression", None)
            if true_action is not None and not isinstance(
                true_action, (bool, builtins.int, builtins.float, builtins.str)
            ):
                label = _renforge_observe_format_action(true_action, depth + 1)
                if label:
                    return label
        if name == "Function":
            return _renforge_observe_format_function(value, depth)
        # Ren'Py mangles a class named __X in game/common .rpy files into
        # _m1_<file>__X (MusicRoom.Play's _m1_00musicroom__MusicRoomPlay).
        mangled = _RENFORGE_MANGLED_NAME.match(name)
        if mangled:
            name = mangled.group(1)
        parts = []
        for key in ("screen", "label", "name", "variable", "field", "filename", "url", "page", "slot", "key"):
            try:
                field = getattr(value, key)
            except Exception:
                continue
            quoted = _renforge_observe_quote(field)
            if quoted:
                parts.append(quoted)
        for extra_name in ("value", "true_value", "false_value"):
            try:
                extra = getattr(value, extra_name)
            except Exception:
                continue
            quoted = _renforge_observe_quote(extra, allow_bool=True)
            if quoted:
                parts.append(quoted)
        if parts:
            return "%s(%s)" % (name, ", ".join(parts))
        return str(name)


    def _renforge_observe_action_class(widget):
        if widget is None:
            return None
        attrs = ("action", "clicked")
        if _renforge_is_engine_widget(widget, "Drag"):
            attrs = ("dragged", "clicked", "action")
        for attr in attrs:
            value = getattr(widget, attr, None)
            if value is None:
                continue
            label = _renforge_observe_format_action(value)
            if label:
                return label
        return None


    def _renforge_observe_action_attr(widget, attr):
        if widget is None:
            return None
        value = getattr(widget, attr, None)
        if value is None:
            return None
        return _renforge_observe_format_action(value)


    def _renforge_observe_action_selected(action, depth):
        if action is None or depth > _RENFORGE_OBSERVE_DEPTH:
            return None
        if _renforge_observe_is_sequence(action):
            for item in action:
                try:
                    item_name = item.__class__.__name__
                except Exception:
                    item_name = ""
                if item_name == "SelectedIf":
                    return _renforge_observe_action_selected(
                        getattr(item, "expression", None), depth + 1
                    )
            found = None
            for item in action:
                selected = _renforge_observe_action_selected(item, depth + 1)
                if selected is True:
                    return True
                if selected is False and found is None:
                    found = False
            return found
        try:
            name = action.__class__.__name__
        except Exception:
            return None
        if name == "SelectedIf":
            return _renforge_observe_action_selected(getattr(action, "expression", None), depth + 1)
        getter = getattr(action, "get_selected", None)
        if not callable(getter):
            return None
        try:
            result = getter()
        except Exception:
            return None
        if isinstance(result, bool):
            return result
        return None


    def _renforge_observe_selected(widget):
        if widget is None:
            return None
        chosen = getattr(widget, "selected", None)
        if isinstance(chosen, bool):
            return chosen
        action = None
        for attr in ("action", "clicked"):
            value = getattr(widget, attr, None)
            if value is not None:
                action = value
                break
        return _renforge_observe_action_selected(action, 0)


    def _renforge_observe_number(value):
        if isinstance(value, bool) or not isinstance(value, (builtins.int, builtins.float)):
            return None
        return value


    def _renforge_observe_axis(adjustment):
        if adjustment is None:
            return None
        value = _renforge_observe_number(getattr(adjustment, "value", None))
        span = _renforge_observe_number(getattr(adjustment, "range", None))
        step = _renforge_observe_number(getattr(adjustment, "step", None))
        page = _renforge_observe_number(getattr(adjustment, "page", None))
        if value is None and span is None and step is None and page is None:
            return None
        return {"value": value, "range": span, "step": step, "page": page}


    def _renforge_observe_adjustment(widget):
        if widget is None:
            return None
        if _renforge_is_engine_widget(widget, "Viewport"):
            axes = {}
            horizontal = _renforge_observe_axis(getattr(widget, "xadjustment", None))
            vertical = _renforge_observe_axis(getattr(widget, "yadjustment", None))
            if horizontal is not None:
                axes["x"] = horizontal
            if vertical is not None:
                axes["y"] = vertical
            return axes or None
        return _renforge_observe_axis(getattr(widget, "adjustment", None))


    def _renforge_observe_drag_name(widget):
        if not _renforge_is_engine_widget(widget, "Drag"):
            return None
        name = getattr(widget, "drag_name", None)
        if isinstance(name, builtins.str) and name.strip():
            return name.strip()
        return None


    def _renforge_observe_drag_image(widget):
        if not _renforge_is_engine_widget(widget, "Drag"):
            return None
        return _renforge_displayable_filename(getattr(widget, "child", None))


    def _renforge_observe_tooltip(widget):
        # The `tooltip` property: Ren'Py's own description of a button, often
        # the only words an image-only button has.
        getter = getattr(widget, "_get_tooltip", None)
        try:
            tip = getter() if callable(getter) else None
        except Exception:
            return None
        if not isinstance(tip, builtins.str):
            return None
        return _renforge_strip_say_tags(tip).strip() or None


    def _renforge_observe_control_facts(widget):
        return {
            "tooltip": _renforge_observe_tooltip(widget),
            "drag_name": _renforge_observe_drag_name(widget),
            "alternate": _renforge_observe_action_attr(widget, "alternate"),
            "hovered": _renforge_observe_action_attr(widget, "hovered"),
            "selected": _renforge_observe_selected(widget),
            "adjustment": _renforge_observe_adjustment(widget),
        }


    def _renforge_display_name(who):
        """The name a speaker draws in the say window, or None.

        Character.name is that name; the say callback receives the Character
        object, not the string. A dynamic Character's name is an expression
        (DynamicCharacter("player_name")), resolved the way the engine would
        but without running game code; a name that cannot be read that way is
        None rather than the expression text.
        """
        if who is None:
            return None
        if isinstance(who, builtins.str):
            text = _renforge_render_say_text(who).strip()
            return text or None
        name = getattr(who, "name", None)
        try:
            dynamic = bool(getattr(who, "dynamic", False))
        except Exception:
            dynamic = False
        if dynamic:
            found, value = _renforge_store_expression(name)
            if not found or not isinstance(value, builtins.str):
                return None
            text = _renforge_render_say_text(value).strip()
            return text or None
        if isinstance(name, builtins.str):
            text = _renforge_render_say_text(name).strip()
            return text or None
        if name is not None and name is not who:
            nested = _renforge_display_name(name)
            if nested:
                return nested
        text = _renforge_focus_text(who)
        if text and text != getattr(getattr(who, "__class__", None), "__name__", ""):
            return text
        return None

    def _renforge_render_say_text(text):
        substitute = getattr(renpy, "substitute", None)
        if isinstance(text, builtins.str) and callable(substitute):
            try:
                return substitute(text)
            except Exception:
                pass
        return text


    def _renforge_flatten_what(value):
        if isinstance(value, builtins.str):
            return value
        if value is None or isinstance(value, (builtins.dict, builtins.int, bool)):
            return None
        if not _renforge_observe_is_sequence(value):
            return None
        parts = []
        for part in value:
            flat = _renforge_flatten_what(part)
            if flat:
                parts.append(flat)
        if not parts:
            return None
        return "".join(parts)


    def _renforge_store_expression(expr):
        """Resolve a say ``who`` or dynamic-name expression without running game code.

        The engine evaluates these with eval_who and py_eval. The bridge only
        reads: a string literal (``"Mysterious voice"``) is its own value; a
        name is looked up in the ``character`` namespace, then in the store,
        the order eval_who uses; a dotted path follows instance fields and
        dict keys. Calls, operators and properties are never evaluated.
        Returns ``(found, value)``.
        """
        if not isinstance(expr, builtins.str) or not expr.strip():
            return False, None
        text = expr.strip()
        import ast as _ast
        try:
            literal = _ast.literal_eval(text)
        except Exception:
            literal = None
        if isinstance(literal, builtins.str):
            return True, literal
        parts = text.split(".")
        if not builtins.all(part.isidentifier() for part in parts):
            return False, None
        value = None
        found = False
        try:
            namespaces = renpy.python.store_dicts
        except Exception:
            namespaces = None
        if len(parts) == 1 and namespaces is not None:
            # `define character.e = Character(...)` lives in store.character.
            try:
                characters = namespaces.get("store.character")
                value = characters.get(parts[0]) if characters is not None else None
            except Exception:
                value = None
            found = value is not None
        if not found:
            try:
                fields = vars(renpy.store)
            except Exception:
                return False, None
            if parts[0] not in fields:
                return False, None
            value = fields[parts[0]]
        for part in parts[1:]:
            fields = getattr(value, "__dict__", None)
            if isinstance(value, builtins.dict) and part in value:
                value = value[part]
            elif isinstance(fields, builtins.dict) and part in fields:
                value = fields[part]
            else:
                return False, None
        return True, value


    def _renforge_character_display_name(who_expr):
        """The display name for a say statement's who expression, or None.

        ast.Say stores the expression (``e``, ``"Mysterious voice"``), not the
        speaker, so it is resolved with _renforge_store_expression.
        """
        if who_expr is None:
            return None
        if not isinstance(who_expr, builtins.str):
            return _renforge_display_name(who_expr)
        found, character = _renforge_store_expression(who_expr)
        if not found:
            return None
        return _renforge_display_name(character)


    def _renforge_observe_screen_node_name(node):
        name = getattr(node, "screen_name", None)
        if _renforge_observe_is_sequence(name):
            name = name[0] if name else None
        if isinstance(name, builtins.str) and name:
            return name
        return None


    def _renforge_is_input_displayable(node):
        """Whether ``node`` is Ren'Py's Input, a subclass included."""
        return _renforge_is_engine_widget(node, "Input")


    def _renforge_input_prompt(screen_node, widget):
        scope = getattr(screen_node, "scope", None) if screen_node is not None else None
        if isinstance(scope, builtins.dict):
            prompt = scope.get("prompt")
            if isinstance(prompt, builtins.str) and prompt.strip():
                # As drawn: "[player_name]" substituted, text tags removed.
                return _renforge_strip_say_tags(_renforge_render_say_text(prompt)).strip() or None
        return None


    def _renforge_input_bounds():
        rect = None
        try:
            rect = renpy.display.interface.text_rect
        except Exception:
            rect = None
        try:
            override = getattr(renpy.store, "_text_rect", None)
        except Exception:
            override = None
        if override is not None:
            rect = override
        if not _renforge_observe_is_sequence(rect) or len(rect) != 4:
            return None
        try:
            x, y, w, h = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
        except (TypeError, ValueError, OverflowError):
            return None
        if w <= 0 or h <= 0:
            return None
        return {"x": x, "y": y, "width": w, "height": h}


    def _renforge_observe_input_records(seen_widgets, inventory=None):
        if inventory is None:
            inventory = _renforge_scene_inventory()
        found = []
        for node, _layer, screen_name, screen_node, _parents in inventory["nodes"]:
            if id(node) in seen_widgets or not _renforge_is_input_displayable(node):
                continue
            found.append((node, screen_name, screen_node))
        bounds = _renforge_input_bounds() if len(found) == 1 else None
        records = []
        for ordinal, (widget, screen_name, screen_node) in enumerate(found):
            editable = getattr(widget, "editable", True)
            try:
                enabled = bool(editable)
            except Exception:
                enabled = True
            record = {
                "screen": screen_name,
                "role": "input",
                "text": _renforge_input_prompt(screen_node, widget),
                "enabled": enabled,
                "clickable": enabled,
                "covered": False,
                "visible": True,
                "image_name": None,
                "menu_index": None,
                "ordinal": ordinal,
                "action": _renforge_observe_action_class(widget),
            }
            if bounds is not None:
                record["bounds"] = bounds
                record["center"] = _renforge_bounds_center(bounds)
            records.append((widget, record))
        return records


    def _renforge_strip_say_tags(value):
        return _renforge_runtime.interaction.strip_text_tags(value)

    def _renforge_say_is_stale(bridge):
        """Whether the stored line belongs to an earlier interaction than this one.

        The say callbacks set ``say_stale_from``; before any line it is unset
        and nothing counts as stale.
        """
        stale_from = getattr(bridge, "say_stale_from", None) if bridge is not None else None
        if not isinstance(stale_from, builtins.int):
            return False
        return int(getattr(bridge, "observe_generation", 0) or 0) >= stale_from

    def _renforge_observe_dialogue(screen_nodes, say_dismiss):
        """The current line as {"who", "what"}, or None when there is none.

        A say screen's scope (or its rendered who/what widgets) wins. Without
        one, the line the say callbacks stored is used while its statement
        is still this interaction's, which also covers NVL and custom say
        screens; a hard pause shows no dialogue.
        """
        # last_say survives the say window. Publish it only while this
        # interaction is still that line, or while a say screen is showing.
        scoped_who = None
        scoped_who_set = False
        scoped_what = None
        scoped_rank = None
        for node in screen_nodes or []:
            if getattr(node, "hiding", False):
                continue
            scope = getattr(node, "scope", None)
            if not isinstance(scope, builtins.dict) or "what" not in scope:
                continue
            name = _renforge_observe_screen_node_name(node)
            if "who" not in scope and name not in ("say", "nvl", "nvl_dialogue"):
                continue
            rank = 0 if name in ("say", "nvl", "nvl_dialogue") else 1
            if scoped_rank is not None and rank >= scoped_rank:
                continue
            scoped_rank = rank
            scoped_who_set = "who" in scope
            scoped_who = scope.get("who") if scoped_who_set else None
            scoped_what = _renforge_flatten_what(scope.get("what"))
            widgets = getattr(node, "widgets", None)
            who_widget = widgets.get("who") if isinstance(widgets, builtins.dict) else None
            if who_widget is not None:
                rendered_who = _renforge_observe_text_string(who_widget, max_chars=None)
                if rendered_who is not None:
                    scoped_who = rendered_who
                    scoped_who_set = True
            what_widget = widgets.get("what") if isinstance(widgets, builtins.dict) else None
            if what_widget is not None:
                rendered_what = _renforge_observe_text_string(what_widget, max_chars=None)
                if rendered_what is not None:
                    scoped_what = rendered_what
        # A hard pause (dismiss_hard_pause alone) shows no say window: free
        # roam that waits on one would otherwise repeat the scene's last line.
        showing = scoped_what is not None or scoped_who_set or say_dismiss == "dismiss"
        if not showing:
            return None
        bridge = _renforge_runtime.bridge

        def speaker_from_store():
            try:
                store = renpy.store
                has_who = hasattr(store, "_last_say_who")
            except Exception:
                return _renforge_display_name(getattr(bridge, "last_who", None)) if bridge is not None else None
            if has_who:
                return _renforge_character_display_name(getattr(store, "_last_say_who", None))
            if bridge is None:
                return None
            return _renforge_display_name(getattr(bridge, "last_who", None))

        if scoped_who_set or scoped_what is not None:
            # A say screen that passes who=None is the narrator. Do not reuse
            # the previous speaker. A screen with only `what` still can.
            who = _renforge_display_name(scoped_who) if scoped_who_set else speaker_from_store()
            what = scoped_what
        else:
            # No say screen carries the line. The stored line is still this
            # interaction's dialogue only while its say statement runs; a soft
            # pause after it also dismisses with "dismiss".
            if _renforge_say_is_stale(bridge):
                return None
            # Character callbacks on 8.3+ receive ``what``. 8.0–8.2 call the
            # same hook without it, while the statement still stores the line
            # on ``_last_say_what``. Prefer the callback copy when it exists.
            what = bridge.last_say if bridge is not None and isinstance(bridge.last_say, builtins.str) else None
            if not isinstance(what, builtins.str) or not what:
                try:
                    stored = getattr(renpy.store, "_last_say_what", None)
                except Exception:
                    stored = None
                if isinstance(stored, builtins.str) and stored:
                    what = stored
            who = speaker_from_store()
        if isinstance(what, builtins.str):
            what = _renforge_strip_say_tags(what) or None
        if who is None and what is None:
            return None
        return {"who": who, "what": what}


    def _renforge_menu_entries(inventory):
        screens = []
        seen = set()
        for node, _layer, _name, screen, _parents in inventory["nodes"]:
            if screen is not None and id(screen) not in seen:
                screens.append(screen)
                seen.add(id(screen))
        for focus in getattr(getattr(renpy.display, "focus", None), "focus_list", ()):
            screen = getattr(focus, "screen", None)
            if screen is not None and id(screen) not in seen and not _renforge_screen_leaving(screen):
                screens.append(screen)
                seen.add(id(screen))
        entries = []
        for screen in screens:
            scope = getattr(screen, "scope", None) or {}
            items = scope.get("items") if isinstance(scope, builtins.dict) else None
            if not _renforge_observe_is_sequence(items):
                continue
            index = 0
            for item_number, item in enumerate(items):
                if item_number >= _RENFORGE_OBSERVE_MAX_CONTROLS or len(entries) >= _RENFORGE_OBSERVE_MAX_CONTROLS:
                    inventory["menu_truncated"] = True
                    break
                action = getattr(item, "action", None)
                caption = getattr(item, "caption", None)
                if action is None or caption is None:
                    continue
                # Show the caption as the screen does, with [variables] substituted.
                substitute = getattr(getattr(renpy, "substitutions", None), "substitute", None)
                try:
                    if callable(substitute) and isinstance(caption, builtins.str):
                        caption = substitute(caption)[0]
                except Exception:
                    pass
                # Read as the player sees it: "{size=60}No.{/size}" is "No.".
                entries.append((screen, index, _renforge_strip_say_tags(str(caption)) or str(caption), action))
                index += 1
        return entries


    # Ren'Py interface actions: menus, skipping, rollback, saves, preferences.
    # They never advance the story, so a say screen holding them is still
    # dismissable dialogue.
    _RENFORGE_CHROME_ACTIONS = frozenset((
        "ShowMenu", "Skip", "Rollback", "RollForward", "HideInterface",
        "QuickSave", "QuickLoad", "FileSave", "FileLoad", "FileDelete",
        "Help", "Quit", "MainMenu", "Screenshot", "OpenURL",
        "SetMute", "ToggleMute", "SetMixer",
    ))


    # Engine actions that change no game state a story choice could depend on.
    _RENFORGE_NEUTRAL_ACTIONS = frozenset((
        "SensitiveIf", "SelectedIf", "Play", "Queue", "Stop", "PauseAudio",
    ))


    # The screens ShowMenu opens as Ren'Py's game menu (None is its default
    # screen). interaction._GAME_MENU_SCREENS is a different list: screens a
    # player is on whose buttons stay chrome, so it adds main_menu,
    # navigation, confirm and yesno_prompt, which are not ShowMenu targets.
    _RENFORGE_GAME_MENU_SCREENS = frozenset((
        None, "game_menu", "save", "load", "preferences", "history", "help", "about",
    ))


    def _renforge_action_list(action):
        """The actions a button runs, flattened as Ren'Py's run() nests lists."""
        if action is None:
            return []
        if not _renforge_observe_is_sequence(action):
            return [action]
        flat = []
        for item in action:
            flat.extend(_renforge_action_list(item))
        return flat


    def _renforge_engine_chrome_action(actions):
        """True when every action is one of Ren'Py's own interface actions.

        The class must come from Ren'Py's common library, so a game class that
        merely shares a name does not count.
        """
        if not actions:
            return False
        # FileTakeScreenshot captures the save thumbnail just before a game
        # opens its menu, even a custom one such as Show("pause_menu").
        if any(type(action).__name__ == "FileTakeScreenshot" and _renforge_engine_class(type(action))
               for action in actions):
            return True
        # SensitiveIf/SelectedIf only set a button's state, and a click sound
        # (Play("sound", ...)) only plays; neither makes a menu button a story
        # decision.
        actions = [action for action in actions
                   if not (type(action).__name__ in _RENFORGE_NEUTRAL_ACTIONS
                           and _renforge_engine_class(type(action)))]
        if not actions:
            return False
        # Showing or hiding a screen beside an interface action is that
        # button's own indicator: a fast-forward button that runs
        # Show("skip_indicator") with Skip(). Skip only turns sensitive while
        # skipping is allowed, so as a decision it stopped progression (which
        # allows skipping) at once.
        if any(type(action).__name__ in _RENFORGE_CHROME_ACTIONS and _renforge_engine_class(type(action))
               for action in actions):
            actions = [action for action in actions
                       if not (type(action).__name__ in ("Show", "Hide", "ToggleScreen")
                               and _renforge_engine_class(type(action)))]
        for action in actions:
            cls = type(action)
            if cls.__name__ not in _RENFORGE_CHROME_ACTIONS:
                return False
            # ShowMenu("map") is gameplay; only Ren'Py's standard game-menu
            # screens are interface chrome.
            if cls.__name__ == "ShowMenu" and getattr(action, "screen", None) not in _RENFORGE_GAME_MENU_SCREENS:
                return False
            if not _renforge_engine_class(cls):
                return False
        return True


    def _renforge_inert_action(actions):
        """True when every action is NullAction or a neutral engine action.

        NullAction exists to give a button hover effects and does nothing on
        a click (a tutorial's decorative icons), so it never ends a pause.
        """
        return bool(actions) and all(
            type(action).__name__ in _RENFORGE_NEUTRAL_ACTIONS | {"NullAction"}
            and _renforge_engine_class(type(action))
            for action in actions)


    def _renforge_engine_class(cls):
        """True when the class is defined in Ren'Py's common library."""
        code = next((getattr(value, "__code__", None) for value in vars(cls).values()
                     if getattr(value, "__code__", None) is not None), None)
        return "renpy/common/" in str(getattr(code, "co_filename", "")).replace("\\", "/")


    def _renforge_source_tuple(displayable):
        """``(file, line)`` of the screen-language statement that built it.

        Screen language records the location on ``d._main or d``: a
        textbutton's location lives on its Text child.
        """
        location = getattr(displayable, "_location", None)
        if location is None:
            location = getattr(getattr(displayable, "_main", None), "_location", None)
        return _renforge_explain_location_tuple(location)


    def _renforge_source_location(displayable):
        """``file:line`` of the screen-language statement that built it."""
        location = _renforge_source_tuple(displayable)
        if location is None:
            return None
        return "%s:%d" % location


    def _renforge_action_contains(container, wanted):
        if container is wanted:
            return True
        if _renforge_observe_is_sequence(container):
            return any(item is wanted for item in container)
        return False


    def _renforge_action_wraps(container, wanted):
        """A Function action handed the menu item or its action as an argument.

        A choice screen may route each item through its own callback
        (``action Function(on_choice, item=i)``); that button is the item.
        """
        for action in _renforge_action_list(container):
            args = getattr(action, "args", None)
            kwargs = getattr(action, "kwargs", None)
            values = builtins.list(args) if _renforge_observe_is_sequence(args) else []
            if isinstance(kwargs, builtins.dict):
                values.extend(kwargs.values())
            for value in values:
                if value is wanted or getattr(value, "action", None) is wanted:
                    return True
        return False


    def _renforge_focus_clone(focus, widget, screen, **extra):
        """A focus stand-in for ``widget`` on ``screen`` with ``focus``'s rectangle."""
        return types.SimpleNamespace(widget=widget, screen=screen,
            x=getattr(focus, "x", None), y=getattr(focus, "y", None),
            w=getattr(focus, "w", None), h=getattr(focus, "h", None), **extra)


    def _renforge_bounds_center(bounds):
        """The center point of ``{"x", "y", "width", "height"}`` bounds, or None."""
        if not bounds:
            return None
        return {"x": bounds["x"] + bounds["width"] // 2, "y": bounds["y"] + bounds["height"] // 2}


    def _renforge_safe_sensitive(sensitivity, action):
        """Ren'Py's is_sensitive for ``action``; True when there is none or it raises."""
        if not callable(sensitivity):
            return True
        try:
            return bool(sensitivity(action))
        except Exception:
            return True


    def _renforge_focus_list_records():
        """(focus, record) for Ren'Py's focus list: the controls it can focus."""
        for ordinal, focus in enumerate(getattr(getattr(renpy.display, "focus", None), "focus_list", ())):
            widget = getattr(focus, "widget", None)
            if widget is None:
                continue
            values = [getattr(focus, key, None) for key in ("x", "y", "w", "h")]
            bounds = None
            if all(value is not None for value in values):
                try:
                    x, y, w, h = [int(value) for value in values]
                    if w <= 0 or h <= 0:
                        continue
                    bounds = {"x": x, "y": y, "width": w, "height": h}
                except (TypeError, ValueError, OverflowError):
                    pass
            role = _renforge_observe_role(focus, widget)
            if bounds is None and not _renforge_observe_operations(_renforge_observe_normalize_role(role)):
                continue
            name = _renforge_screen_name(focus)
            # A viewport scrolls content; that content is readout, not its label.
            is_viewport = _renforge_observe_normalize_role(role) == "viewport"
            yield focus, {
                "screen": name, "role": role,
                "text": None if is_viewport else (_renforge_focus_text(widget) or None),
                "enabled": bool(_renforge_focus_enabled(focus, widget)),
                "clickable": True, "covered": False, "visible": True,
                "image_name": _renforge_observe_image_name(widget) or _renforge_observe_drag_image(widget),
                "menu_index": None, "ordinal": ordinal,
                "action": _renforge_observe_action_class(widget), "bounds": bounds,
            }


    def _renforge_unfocused_button_records(inventory, seen_widgets):
        """(focus, record) for drawn Buttons the focus list left out.

        Ren'Py leaves a Button out of the focus list when it has no action
        (insensitive) or a modal screen hides it. ``ordinal`` is set by the
        caller; ``seen_widgets`` is read as the caller fills it.
        """
        button_type = getattr(getattr(renpy.display, "behavior", None), "Button", None)
        if not isinstance(button_type, type):
            return
        for widget, layer, name, screen, _parents in inventory["nodes"]:
            if id(widget) in seen_widgets or not isinstance(widget, button_type):
                continue
            geometry = _renforge_widget_geometry(widget, inventory)
            if geometry["visible"] is not True:
                continue
            focus = types.SimpleNamespace(widget=widget, screen=screen)
            yield focus, {"screen": name, "layer": layer, "role": "button",
                "text": _renforge_focus_text(widget) or None,
                "enabled": (getattr(widget, "clicked", None) is not None
                            and bool(_renforge_focus_enabled(focus, widget))),
                "clickable": True, "covered": False,
                "image_name": _renforge_observe_image_name(widget),
                "menu_index": None, "ordinal": None,
                "action": _renforge_observe_action_class(widget), **geometry}


    def _renforge_custom_action_records(inventory, seen_widgets):
        """(focus, record) for a game's own displayables that hold Ren'Py Actions.

        Such a widget keeps them in ``action`` (or one ``*click*`` attribute,
        such as ``click_action``) and follows the engine's button convention:
        running the action is the click. ``_renforge_custom_actions`` decides
        what counts, as it does for the click; discovery also needs
        ``renpy.ui.Action``, so a bare callable is never listed.
        """
        if not isinstance(getattr(getattr(renpy, "ui", None), "Action", None), type):
            return
        sensitivity = getattr(getattr(renpy.display, "behavior", None), "is_sensitive", None)
        for widget, layer, name, screen, _parents in inventory["nodes"]:
            if id(widget) in seen_widgets or type(widget).__module__.startswith("renpy."):
                continue
            found = _renforge_custom_actions(widget)
            if found is None:
                continue
            value, actions = found
            geometry = _renforge_widget_geometry(widget, inventory)
            if geometry["visible"] is not True:
                continue
            # As with Ren'Py buttons, no action means insensitive.
            enabled = bool(actions) and _renforge_safe_sensitive(sensitivity, value)
            focus = types.SimpleNamespace(widget=widget, screen=screen,
                                          custom_action=value if actions else None)
            yield focus, {
                "screen": name, "layer": layer, "role": "button",
                "text": _renforge_focus_text(widget) or None,
                "enabled": enabled, "clickable": enabled, "covered": False,
                "semantic": True,
                "image_name": _renforge_observe_image_name(widget),
                "menu_index": None, "ordinal": None,
                "action": _renforge_observe_format_action(value) if value is not None else None,
                **geometry}


    def _renforge_click_anywhere_records(inventory, seen_widgets):
        """(focus, record) for a screen's left-click key bindings.

        A screen's `key "mousedown_1" action ...` makes a click anywhere its
        control (a tutorial screen that advances only so). Act's key/
        pseudo-controls press keyboard keys, so a left-click binding is listed
        as a control that runs its action.
        """
        behavior = getattr(renpy.display, "behavior", None)
        keymap_type = getattr(behavior, "Keymap", None)
        if not isinstance(keymap_type, type):
            return
        sensitivity = getattr(behavior, "is_sensitive", None)
        for widget, layer, name, screen, _parents in inventory["nodes"]:
            if screen is None or id(widget) in seen_widgets or not isinstance(widget, keymap_type):
                continue
            bindings = getattr(widget, "keymap", None)
            if not isinstance(bindings, builtins.dict):
                continue
            for binding in ("mousedown_1", "mouseup_1"):
                action = bindings.get(binding)
                if action is None:
                    continue
                enabled = _renforge_safe_sensitive(sensitivity, action)
                yield types.SimpleNamespace(widget=widget, screen=screen, action=action), {
                    "screen": name, "layer": layer, "role": "button",
                    "text": "click anywhere (%s)" % binding,
                    "enabled": enabled, "clickable": enabled, "covered": False,
                    "semantic": True, "visible": True,
                    "bounds": None, "bounds_reason": "a key binding: a click anywhere on the screen",
                    "image_name": None, "menu_index": None, "ordinal": None,
                    "action": _renforge_observe_format_action(action)}
                break


    def _renforge_add_unmatched_menu_entries(pairs, unmatched, inventory):
        """Add menu entries no control matched to ``pairs``.

        A choice outside the focus list (clipped, or in a transformed
        viewport) usually still has its button in the screen's tree; that
        button gives the entry its source line. A button drawn with no action
        is the entry, disabled.
        """
        menu_buttons = builtins.dict()
        if unmatched:
            owners = builtins.set(id(entry[0]) for entry in unmatched)
            for node, _layer, _name, screen_node, _parents in inventory["nodes"]:
                if id(screen_node) in owners and (getattr(node, "action", None) is not None
                                                  or getattr(node, "clicked", None) is not None):
                    menu_buttons.setdefault(id(screen_node), builtins.list()).append(node)
        sensitivity = getattr(getattr(renpy.display, "behavior", None), "is_sensitive", None)
        for owner, index, caption, action in unmatched:
            enabled = _renforge_safe_sensitive(sensitivity, action)
            name = _renforge_observe_screen_node_name(owner)
            button = next((node for node in menu_buttons.get(id(owner), ())
                           if _renforge_action_contains(getattr(node, "action", None)
                                                        or getattr(node, "clicked", None), action)), None)
            if button is None:
                # A choice screen can draw an item as a button with no action
                # (`textbutton i.caption action None sensitive False` for
                # disabled=...). That button is the item: it cannot be chosen.
                drawn = next((pair for pair in pairs
                              if getattr(pair[0], "screen", None) is owner
                              and getattr(getattr(pair[0], "widget", None), "clicked", 0) is None
                              and pair[1].get("text") == caption), None)
                if drawn is not None:
                    drawn[1].update({"menu_index": index, "semantic": True, "enabled": False,
                                     "clickable": False, "operations": ["click"]})
                    pairs[pairs.index(drawn)] = (_renforge_focus_clone(
                        drawn[0], drawn[0].widget, owner, action=action, menu_action=action), drawn[1])
                    continue
            focus = types.SimpleNamespace(widget=None, screen=owner, action=action, menu_action=action)
            pairs.append((focus, {"screen": name, "layer": getattr(owner, "layer", None),
                "role": "button", "type": "button", "text": caption,
                "enabled": enabled, "clickable": enabled, "covered": False,
                "visible": None, "bounds": None, "bounds_reason": "menu entry has no focus rectangle",
                "semantic": True, "menu_index": index, "ordinal": len(pairs),
                "action": action.__class__.__name__, "operations": ["click"],
                "source": _renforge_source_location(button) if button is not None else None,
                "coordinate_space": "logical"}))


    def _renforge_observe_focus_pairs(inventory=None):
        """Collect current controls once; observe and the UI-element tools both read these pairs.

        Returns ``(pairs, truncated)``: ``(focus, record)`` for the focus list,
        inputs, bars/viewports/drags, unfocused Buttons, a game's own action
        widgets, click-anywhere key bindings and unmatched menu entries, in
        that order, capped at _RENFORGE_OBSERVE_MAX_CONTROLS.
        """
        if inventory is None:
            inventory = _renforge_scene_inventory()
        pairs = []
        seen_widgets = set()
        focus_truncated = False
        menu = _renforge_menu_entries(inventory)
        matched_menu = set()
        contexts = {id(node): (layer, name, screen) for node, layer, name, screen, _parents in inventory["nodes"]}

        def add(focus, record):
            widget = getattr(focus, "widget", None)
            context = contexts.get(id(widget))
            if context:
                record["layer"] = context[0]
                if getattr(focus, "screen", None) is None:
                    focus = _renforge_focus_clone(focus, widget, context[2])
            screen = getattr(focus, "screen", None)
            value = _renforge_focus_action_value(focus) if menu else None
            for entry_number, (owner, index, caption, action) in enumerate(menu):
                if owner is not screen:
                    continue
                if _renforge_action_contains(value, action):
                    matched_menu.add(entry_number)
                    record["menu_index"] = index
                    record["text"] = caption
                    record["semantic"] = True
                    focus = _renforge_focus_clone(focus, widget, owner, action=action, menu_action=action)
                    break
                if _renforge_action_wraps(value, action):
                    # The item, but a click runs the game's own callback.
                    matched_menu.add(entry_number)
                    record["menu_index"] = index
                    record["text"] = caption
                    record["semantic"] = True
                    break
            record["widget_id"] = _renforge_observe_widget_id(focus, widget, inventory["named"])
            record["source"] = _renforge_source_location(widget)
            actions = _renforge_action_list(_renforge_focus_action_value(focus))
            if _renforge_engine_chrome_action(actions):
                record["chrome"] = True
            elif _renforge_inert_action(actions):
                record["inert"] = True
            record["type"] = _renforge_focus_type(focus, widget)
            record["coordinate_space"] = "logical"
            record["zorder"] = _renforge_focus_zorder(focus, widget, len(pairs))
            if widget is not None:
                seen_widgets.add(id(widget))
                geometry = _renforge_widget_geometry(widget, inventory)
                if not record.get("bounds"):
                    record.update(geometry)
                record["occurrences"] = inventory["geometry"]["occurrences"].get(id(widget), [])
                _renforge_observe_apply_facts(record, widget)
                if any(operation in record.get("operations", ()) for operation in ("text", "value", "drop")):
                    record["semantic"] = True
                button_type = getattr(getattr(renpy.display, "behavior", None), "Button", None)
                if (isinstance(button_type, type) and isinstance(widget, button_type)
                        and not getattr(focus, "w", None)
                        and _renforge_focus_action_value(focus) is not None):
                    record["semantic"] = True
            if record.get("bounds"):
                record["center"] = _renforge_bounds_center(record["bounds"])
            pairs.append((focus, record))

        for focus, record in _renforge_focus_list_records():
            if len(pairs) >= _RENFORGE_OBSERVE_MAX_CONTROLS:
                focus_truncated = True
                break
            add(focus, record)
        for widget, record in _renforge_observe_input_records(seen_widgets, inventory):
            add(types.SimpleNamespace(widget=widget, screen=contexts.get(id(widget), (None, None, None))[2]), record)
        for focus, record in _renforge_observe_kinetic_records(seen_widgets, inventory):
            add(focus, record)
        # The generators read seen_widgets lazily, so each sees the widgets
        # every earlier collector added.
        for collect in (_renforge_unfocused_button_records, _renforge_custom_action_records,
                        _renforge_click_anywhere_records):
            for focus, record in collect(inventory, seen_widgets):
                record["ordinal"] = len(pairs)
                add(focus, record)
        unmatched = [entry for number, entry in enumerate(menu) if number not in matched_menu]
        _renforge_add_unmatched_menu_entries(pairs, unmatched, inventory)
        _renforge_mark_coverage(pairs, inventory)
        truncated = (focus_truncated or len(pairs) > _RENFORGE_OBSERVE_MAX_CONTROLS
                     or inventory.get("menu_truncated", False) or inventory["geometry"]["truncated"])
        pairs = pairs[:_RENFORGE_OBSERVE_MAX_CONTROLS]
        assigned = _renforge_runtime.interaction.assign_control_ids([record for _focus, record in pairs])
        for (_focus, record), identified in zip(pairs, assigned):
            record.update(identified)
        return pairs, truncated


    # Keysym modifiers a plain key press satisfies.
    _RENFORGE_PLAIN_KEY_MODIFIERS = ("keydown", "any", "anyrepeat", "anymod", "noshift", "nocaps", "nonum")

    def _renforge_binding_act_key(binding, act_names):
        """The renforge_act key name that fires a keysym, or None.

        `K_SPACE` and `keydown_K_d` name physical keys; a bare `d` matches the
        typed character. Bindings that need a modifier, a release or a
        repeat cannot be sent.
        """
        parts = binding.split("_")
        while len(parts) > 1 and parts[0] in _RENFORGE_PLAIN_KEY_MODIFIERS:
            parts.pop(0)
        key = "_".join(parts)
        if key in act_names:
            return act_names[key]
        if len(key) == 1 and key in _RENFORGE_ACT_KEY_LETTERS:
            return key
        return None


    def _renforge_key_bindings(inventory, limit=20):
        """A screen's own key bindings (`key "K_SPACE" action Return()`).

        Such a screen often continues only on that key, and the only hint
        is text such as "Press SPACE to continue". Each binding names its
        action and, when renforge_act can press the key, the `key/<name>` to
        send. Interface actions (menus, rollback) and actions that do nothing
        are left out, and bindings never count as decisions.
        """
        keymap_type = getattr(getattr(renpy.display, "behavior", None), "Keymap", None)
        if not isinstance(keymap_type, type):
            return []
        act_names = builtins.dict()
        for name, attr in _RENFORGE_ACT_KEY_ATTRS.items():
            act_names.setdefault(attr, name)
        found = []
        seen = builtins.set()
        for widget, _layer, name, screen, _parents in inventory["nodes"]:
            if screen is None or not isinstance(widget, keymap_type):
                continue
            bindings = getattr(widget, "keymap", None)
            if not isinstance(bindings, builtins.dict):
                continue
            for binding, action in bindings.items():
                if not isinstance(binding, builtins.str) or action is None or binding.startswith("mouse"):
                    continue
                actions = _renforge_action_list(action)
                if _renforge_engine_chrome_action(actions) or _renforge_inert_action(actions):
                    continue
                key = (name, binding)
                if key in seen:
                    continue
                seen.add(key)
                act = _renforge_binding_act_key(binding, act_names)
                found.append({"screen": name, "binding": binding,
                              "act": "key/" + act if act else None,
                              "action": _renforge_observe_format_action(action)})
                if len(found) >= limit:
                    return found
        return found


    def _renforge_scene_roots():
        try:
            scene_lists = renpy.game.context().scene_lists
        except Exception:
            return []
        layers = getattr(scene_lists, "layers", None) or {}
        roots = []
        for layer_name, layer in layers.items():
            for entry in layer or []:
                displayable = getattr(entry, "displayable", entry)
                roots.append((layer_name, displayable))
        return roots


    def _renforge_render_geometry(max_nodes=_RENFORGE_OBSERVE_MAX_NODES):
        """Read completed render occurrences without invoking displayable.render.

        Plain builtins are used throughout: store literals are rollback-tracked
        and made this walk about a third slower.

        Render.reverse maps children into their parent's coordinate space;
        child offsets are applied after that transform, as in take_focuses.
        The same widget may appear in several render paths.
        """
        render_mod = getattr(getattr(renpy, "display", None), "render", None)
        root = getattr(render_mod, "screen_render", None)
        width, height = _renforge_scene_window()
        result = builtins.dict(occurrences=builtins.dict(), renders=builtins.dict(), modals=builtins.list(),
                               available=root is not None, truncated=False)
        if root is None:
            return result

        def point(matrix, x, y):
            a, b, c, d, tx, ty = matrix
            return (a*x + c*y + tx, b*x + d*y + ty)

        def compose(left, right):
            a, b, c, d, tx, ty = left
            e, f, g, h, ux, uy = right
            return (a*e+c*f, b*e+d*f, a*g+c*h, b*g+d*h,
                    a*ux+c*uy+tx, b*ux+d*uy+ty)

        def clip_polygon(poly, edge_start, edge_end):
            # Clipping edges are oriented around the transformed rectangle.
            ax, ay = edge_start
            bx, by = edge_end
            def side(p):
                return (bx-ax)*(p[1]-ay) - (by-ay)*(p[0]-ax)
            out = builtins.list()
            if not poly:
                return out
            previous = poly[-1]
            ps = side(previous)
            for current in poly:
                cs = side(current)
                if (cs >= 0) != (ps >= 0):
                    fraction = ps / (ps-cs)
                    out.append((previous[0]+fraction*(current[0]-previous[0]),
                                previous[1]+fraction*(current[1]-previous[1])))
                if cs >= 0:
                    out.append(current)
                previous, ps = current, cs
            return out

        def rectangle(matrix, w, h):
            corners = (point(matrix, 0, 0), point(matrix, w, 0),
                       point(matrix, w, h), point(matrix, 0, h))
            if matrix[0]*matrix[3]-matrix[1]*matrix[2] < 0:
                corners = corners[::-1]
            return corners

        drawn = builtins.dict()

        def draws(render, depth=0):
            """True when the subtree blits a texture or draws a mesh."""
            key = id(render)
            if key in drawn:
                return drawn[key]
            drawn[key] = False
            result = bool(getattr(render, "mesh", None))
            for child in getattr(render, "children", ()) or ():
                if result:
                    break
                target = child[0] if child else None
                if hasattr(target, "render_of"):
                    result = depth < 64 and draws(target, depth + 1)
                elif target is not None:
                    result = True
            drawn[key] = result
            return result

        initial = ((0.0, 0.0), (float(width), 0.0),
                   (float(width), float(height)), (0.0, float(height)))
        stack = builtins.list(((root, (1, 0, 0, 1, 0, 0), initial, (), (), None),))
        visited = 0
        while stack and visited < max_nodes:
            render, matrix, clip, path, parents, inherited_reason = stack.pop()
            if id(render) in parents:
                continue
            visited += 1
            reason = inherited_reason
            try:
                w, h = float(render.width), float(render.height)
                corners = rectangle(matrix, w, h)
                if not all(_renforge_math.isfinite(v) for p in corners for v in p):
                    raise ValueError("non-finite geometry")
                polygon = corners
                for index, edge in enumerate(clip):
                    polygon = clip_polygon(polygon, edge, clip[(index+1) % len(clip)])
                if not clip or getattr(render, "alpha", 1) <= 0 or w <= 0 or h <= 0:
                    polygon = ()
                if polygon and not reason:
                    x = _renforge_math.floor(min(p[0] for p in polygon))
                    y = _renforge_math.floor(min(p[1] for p in polygon))
                    right = _renforge_math.ceil(max(p[0] for p in polygon))
                    bottom = _renforge_math.ceil(max(p[1] for p in polygon))
                    bounds = builtins.dict(x=x, y=y, width=right-x, height=bottom-y)
                    visible = bounds["width"] > 0 and bounds["height"] > 0
                else:
                    bounds, visible = None, None if reason else False
            except Exception:
                bounds, visible, reason = None, None, "unsupported render geometry"
                corners = ()
            empty = None
            if visible and not draws(render):
                # A full-frame canvas that blits nothing is not on screen. The
                # reason describes this render only; children keep their geometry.
                bounds, visible, empty = None, False, "renders nothing"
            occurrence = builtins.dict(bounds=bounds, visible=visible,
                          bounds_reason=reason or empty or (None if visible else "clipped, transparent, or zero-sized"),
                          render_path=builtins.list(path))
            modal = getattr(render, "modal", False)
            if modal and not callable(modal):
                # Render.take_focuses: this hides focus drawn before it.
                result["modals"].append((tuple(path), "window" if modal == "window" else "full", bounds))
            for widget in getattr(render, "render_of", ()) or ():
                result["occurrences"].setdefault(id(widget), builtins.list()).append(builtins.dict(occurrence))
                result["renders"].setdefault(id(widget), builtins.list()).append((render, matrix, occurrence))
            child_clip = clip
            if corners and not reason:
                xclip = bool(getattr(render, "xclipping", False))
                yclip = bool(getattr(render, "yclipping", False))
                if xclip or yclip:
                    child_clip = builtins.list(clip)
                    # Use local edge indices before orientation normalization.
                    raw = (point(matrix, 0, 0), point(matrix, w, 0),
                           point(matrix, w, h), point(matrix, 0, h))
                    for index in ((0, 2) if yclip else ()) + ((1, 3) if xclip else ()):
                        start, end = raw[index], raw[(index+1) % 4]
                        if matrix[0]*matrix[3]-matrix[1]*matrix[2] < 0:
                            start, end = end, start
                        child_clip = clip_polygon(child_clip, start, end)
            if getattr(render, "alpha", 1) <= 0:
                child_clip = ()
            reverse = getattr(render, "reverse", None)
            local = (1, 0, 0, 1, 0, 0)
            if reverse is not None:
                try:
                    origin = reverse.transform(0, 0)
                    xp, yp = reverse.transform(1, 0), reverse.transform(0, 1)
                    local = (xp[0]-origin[0], xp[1]-origin[1], yp[0]-origin[0], yp[1]-origin[1], origin[0], origin[1])
                    if getattr(reverse, "wdx", 0) or getattr(reverse, "wdy", 0):
                        reason = "perspective render geometry unavailable"
                except Exception:
                    reason = "render transform unavailable"
            for index, child in reversed(builtins.list(enumerate(getattr(render, "children", ()) or ()))):
                if len(child) < 3 or not hasattr(child[0], "render_of"):
                    continue
                child_matrix = compose(matrix, compose((1, 0, 0, 1, child[1], child[2]), local))
                stack.append((child[0], child_matrix, child_clip, path+(index,), parents+(id(render),), reason))
        result["truncated"] = bool(stack)
        return result


    def _renforge_widget_geometry(widget, inventory):
        occurrences = inventory["geometry"]["occurrences"].get(id(widget), [])
        visible = next((item for item in occurrences if item["visible"] is True), None)
        return visible or (occurrences[0] if occurrences else {
            "bounds": None, "visible": None,
            "bounds_reason": "not in completed render", "render_path": None})


    def _renforge_screen_leaving(screen):
        """True for a screen that is hiding (HIDE) or the old side of a transition (OLD)."""
        screen_module = getattr(renpy.display, "screen", None)
        return getattr(screen, "phase", None) in (getattr(screen_module, "HIDE", 3), getattr(screen_module, "OLD", 4))


    def _renforge_scene_inventory():
        """Walk every layer's displayables once for one observe or scene_tree.

        Returns ``nodes`` as (node, layer, screen name, screen node, parent
        ids), ``named`` screen widget ids, ``truncated`` and the completed
        render ``geometry``. Screens that are hiding are skipped.
        """
        # Plain builtins: store literals are rollback-tracked and slow here.
        nodes = builtins.list()
        seen = builtins.set()
        named = builtins.dict()
        stack = builtins.list((root, layer, None, None, ()) for layer, root in reversed(_renforge_scene_roots()))
        while stack and len(nodes) < _RENFORGE_OBSERVE_MAX_NODES:
            node, layer, screen_name, screen_node, parents = stack.pop()
            if node is None or id(node) in seen or getattr(node, "hiding", False):
                continue
            seen.add(id(node))
            class_name = node.__class__.__name__
            children = builtins.list()
            extra_widgets = builtins.list()
            if class_name == "ScreenDisplayable":
                # Like ScreenDisplayable.find_focusable: a screen that is hiding
                # (HIDE) or the old side of a transition (OLD) takes no input,
                # even when a lingering hide transform keeps it drawn.
                if _renforge_screen_leaving(node):
                    continue
                screen_node = node
                screen_name = _renforge_observe_screen_node_name(node) or screen_name
                widgets = getattr(node, "widgets", None)
                if isinstance(widgets, builtins.dict):
                    for key, widget in widgets.items():
                        if isinstance(key, builtins.str) and key.strip():
                            named[id(widget)] = key.strip()
                        extra_widgets.append(widget)
            nodes.append((node, layer, screen_name, screen_node, parents))
            sequence = getattr(node, "children", None)
            if _renforge_observe_is_sequence(sequence):
                children.extend(sequence)
            # ImageReference and DynamicImage keep what they draw in target
            # ("show text ..." is one), so follow it too.
            for attr in ("child", "raw_child", "target"):
                child = getattr(node, attr, None)
                if child is not None and (attr != "target" or hasattr(child, "render")):
                    children.append(child)
            children.extend(extra_widgets)
            lineage = parents + (id(node),)
            for child in reversed(children):
                stack.append((child, layer, screen_name, screen_node, lineage))
        return builtins.dict(nodes=nodes, named=named, truncated=bool(stack), geometry=_renforge_render_geometry())


    def _renforge_screen_key(node):
        """A shown screen's key in screen_first_seen.

        The name guards against a freed screen's id being reused by another.
        """
        return (id(node), _renforge_observe_screen_node_name(node))

    def _renforge_screen_first_seen(node, layer, known, generation):
        """The generation a screen first appeared in, from ``known`` stamps.

        An unknown screen is new with ``generation``, except one a loaded save
        restored (by name, rebuilt before the first interaction), which was
        shown before the save: -1.
        """
        key = _renforge_screen_key(node)
        if key in known:
            return known[key]
        bridge = _renforge_runtime.bridge
        restored_at, restored = getattr(bridge, "restored_screens", None) or (None, ())
        if generation == restored_at and (key[1], layer) in restored:
            return -1
        return generation

    def _renforge_stamp_screens():
        """Stamp each shown screen with the interaction it first appeared in.

        Runs from the start-interaction hook, so a screen shown during an
        interaction nobody observed still counts as kept up at the next one.
        Screens no longer on a layer are dropped, so their ids are forgotten.
        """
        bridge = _renforge_runtime.bridge
        if bridge is None:
            return
        generation = getattr(bridge, "observe_generation", 0)
        known = getattr(bridge, "screen_first_seen", None) or {}
        stamped = builtins.dict()
        for layer, node in _renforge_scene_roots():
            if node.__class__.__name__ == "ScreenDisplayable":
                stamped[_renforge_screen_key(node)] = _renforge_screen_first_seen(node, layer, known, generation)
        bridge.screen_first_seen = stamped

    def _renforge_collect_interaction(inventory=None):
        """Screens, say dismiss events and truncation for one observe.

        Returns ``(screens, say_dismiss, truncated, screen_nodes)``; each screen
        record says whether it was kept up from an earlier interaction.
        """
        if inventory is None:
            inventory = _renforge_scene_inventory()
        screens = []
        screen_nodes = []
        layers = []
        dismiss = []
        for node, layer, _name, _screen, _parents in inventory["nodes"]:
            class_name = node.__class__.__name__
            if class_name == "SayBehavior":
                for attr in ("dismiss", "dismiss_unfocused"):
                    dismiss.extend(_renforge_observe_event_names(getattr(node, attr, None)))
            if class_name == "ScreenDisplayable":
                screen_nodes.append(node)
                layers.append(layer)
        # The interaction each shown screen object first appeared in. One
        # Ren'Py still shows from an earlier interaction, and that is not
        # this interaction's own (say, menu, call screen), was kept up across
        # lines: a HUD such as a phone or codex button. The start-interaction
        # hook stamps screens (_renforge_stamp_screens); observe adds only a
        # screen an action showed after this interaction started.
        bridge = _renforge_runtime.bridge
        generation = getattr(bridge, "observe_generation", 0) if bridge is not None else 0
        known = getattr(bridge, "screen_first_seen", None) or {}
        first_seen = {}
        added = {}
        for node, layer in zip(screen_nodes, layers):
            key = _renforge_screen_key(node)
            first_seen[key] = _renforge_screen_first_seen(node, layer, known, generation)
            if key not in known:
                added[key] = first_seen[key]
        if bridge is not None and added:
            merged = builtins.dict(known)
            merged.update(added)
            bridge.screen_first_seen = merged
        seen_screens = {}
        for node, layer in zip(screen_nodes, layers):
            kept_up = (getattr(node, "transient", None) is False
                       and first_seen[_renforge_screen_key(node)] < generation)
            _renforge_observe_record_screen(node, layer, screens, seen_screens, kept_up)
        say_dismiss = _renforge_runtime.interaction.normalize_say_dismiss(dismiss)
        return screens, say_dismiss, inventory["truncated"], screen_nodes

    def _renforge_ensure_after_load_hook():
        """Keep the restored-screen hook in config.after_load_callbacks.

        A game may assign its own list during init, after the bridge added
        the hook (``config.after_load_callbacks = [after_load_setup]``).
        """
        after_load = getattr(renpy.config, "after_load_callbacks", None)
        if isinstance(after_load, builtins.list) and not any(
                getattr(callback, "__name__", "") == "_renforge_mark_restored_screens"
                for callback in after_load):
            after_load.append(_renforge_mark_restored_screens)

    def _renforge_mark_restored_screens():
        """Screens a save restores were shown before it was made, so kept up.

        Loading builds new screen objects, which would otherwise look shown by
        the first interaction after the load.
        """
        bridge = _renforge_runtime.bridge
        if bridge is None:
            return
        try:
            layers = renpy.game.context().scene_lists.layers
        except Exception:
            return
        first_seen = builtins.dict(getattr(bridge, "screen_first_seen", None) or {})
        names = builtins.set()
        for layer, entries in layers.items():
            for entry in entries:
                node = getattr(entry, "displayable", None)
                if node.__class__.__name__ == "ScreenDisplayable":
                    first_seen[_renforge_screen_key(node)] = -1
                    names.add((_renforge_observe_screen_node_name(node), layer))
        bridge.screen_first_seen = first_seen
        # Ren'Py can rebuild a restored screen before the first interaction
        # (changing language on load), so its name counts for that one.
        bridge.restored_screens = (getattr(bridge, "observe_generation", 0) + 1, names)

    def _renforge_observe_record_screen(node, layer_name, screens, seen_screens, kept_up=False):
        name = getattr(node, "screen_name", None)
        if _renforge_observe_is_sequence(name):
            name = name[0] if name else None
        if not isinstance(name, builtins.str) or not name:
            return
        layer = getattr(node, "layer", None)
        if not isinstance(layer, builtins.str):
            layer = layer_name if isinstance(layer_name, builtins.str) else None
        key = (name, layer)
        if key in seen_screens:
            return
        seen_screens[key] = True
        modal = getattr(node, "modal", False)
        screens.append({
            "name": name,
            "layer": layer,
            "modal": modal is True and not callable(modal),
            "kept_up": bool(kept_up),
        })


    def _renforge_observe_text_string(node, max_chars=_RENFORGE_READOUT_TEXT_LIMIT):
        raw = None
        tts = getattr(node, "_tts_all", None)
        if callable(tts):
            try:
                raw = tts(False)
            except TypeError:
                try:
                    raw = tts()
                except Exception:
                    raw = None
            except Exception:
                raw = None
        if not raw:
            raw = getattr(node, "text", None)
        if _renforge_observe_is_sequence(raw):
            parts = []
            for part in raw:
                if isinstance(part, builtins.str):
                    parts.append(part)
            raw = "".join(parts)
        if not isinstance(raw, builtins.str):
            return None
        text = _renforge_strip_say_tags(raw)
        if not text:
            return None
        if max_chars is not None and len(text) > max_chars:
            text = text[: max_chars - 3] + "..."
        return text


    def _renforge_observe_readout(blocked, inventory=None):
        if inventory is None:
            inventory = _renforge_scene_inventory()
        records = []
        truncated = False
        for node, _layer, screen_name, _screen_node, parents in inventory["nodes"]:
            if node.__class__.__name__ != "Text" or id(node) in blocked or any(parent in blocked for parent in parents):
                continue
            text = _renforge_observe_text_string(node)
            if not text:
                continue
            if len(records) >= _RENFORGE_READOUT_LIMIT:
                truncated = True
                break
            record = {"screen": screen_name, "text": text}
            bounds = _renforge_widget_geometry(node, inventory)["bounds"]
            if bounds is not None:
                record["bounds"] = bounds
            records.append(record)
        return records, truncated

    def _renforge_transition_pending():
        # Ren'Py keeps ongoing_transition until the interaction ends, including
        # for a dict `with` that only schedules a layer dissolve onto the next
        # say. The line is past the fade once the shown instance has played
        # for its delay. An unstarted or endless transition stays pending.
        try:
            interface = renpy.display.interface
            ongoing = getattr(interface, "ongoing_transition", None)
        except Exception:
            return False
        if not ongoing:
            return False
        if not isinstance(ongoing, builtins.dict):
            return True
        try:
            now = getattr(interface, "frame_time", None)
            started = getattr(interface, "transition_time", None) or {}
            shown = getattr(interface, "instantiated_transition", None) or {}
        except Exception:
            return True
        for layer in ongoing:
            start = started.get(layer) if isinstance(started, builtins.dict) else None
            inst = shown.get(layer) if isinstance(shown, builtins.dict) else None
            if start is None or inst is None or now is None:
                return True
            delay = getattr(inst, "delay", None)
            if not isinstance(delay, (builtins.int, builtins.float)) or isinstance(delay, bool):
                return True
            try:
                if (now - start) < delay:
                    return True
            except Exception:
                return True
        return False

    # A timed pause this short ends by itself; one that is longer is a place
    # the player waits, and the agent may dismiss it.
    _RENFORGE_SHORT_PAUSE_SECONDS = 5.0
    # A pause returns only on a frame drawn after its delay, so it can outlive
    # its delay a little (`with dissolve` at startup); a modal screen can hold
    # it indefinitely, so stop waiting after this grace.
    _RENFORGE_PAUSE_GRACE_SECONDS = 1.0

    def _renforge_short_pause_pending():
        """True while a short `pause N` or `with` interaction has yet to end.

        interact_core adds a PauseBehavior to the root widget, outside every
        layer, so look for it in the top of the last drawn frame. Screens on
        top can still be clickable while the script is about to move on
        (for example `show black` + `pause 0.5` + `jump`).
        """
        render_mod = getattr(getattr(renpy, "display", None), "render", None)
        root = getattr(render_mod, "screen_render", None)
        pause_type = getattr(getattr(renpy.display, "behavior", None), "PauseBehavior", None)
        interface = getattr(renpy.display, "interface", None)
        started = getattr(interface, "interact_time", None)
        get_time = getattr(getattr(renpy.display, "core", None), "get_time", None)
        if root is None or not isinstance(pause_type, type) or not started or not callable(get_time):
            return False
        stack = [(root, 0)]
        while stack:
            render, depth = stack.pop()
            for shown in getattr(render, "render_of", ()) or ():
                if isinstance(shown, pause_type):
                    delay = getattr(shown, "delay", None)
                    if (isinstance(delay, (builtins.int, builtins.float)) and not isinstance(delay, bool)
                            and 0 < delay <= _RENFORGE_SHORT_PAUSE_SECONDS):
                        try:
                            # interact_time restarts with the interaction, which
                            # a game can restart again and again; a pause can
                            # still last no longer than its delay from when it
                            # was first seen.
                            seen = getattr(_renforge_runtime, "pause_first_seen", None)
                            if not seen or seen[0] is not shown:
                                seen = (shown, get_time())
                                _renforge_runtime.pause_first_seen = seen
                            limit = delay + _RENFORGE_PAUSE_GRACE_SECONDS
                            if get_time() - started < limit and get_time() - seen[1] < limit:
                                return True
                        except Exception:
                            pass
            if depth < 3:
                for child in getattr(render, "children", ()) or ():
                    target = child[0] if child else None
                    if hasattr(target, "render_of"):
                        stack.append((target, depth + 1))
        return False

    def _renforge_first_parse_error():
        errors = getattr(getattr(renpy, "parser", None), "parse_errors", None)
        for error in errors or ():
            for line in builtins.str(error).splitlines():
                if line.strip():
                    return line.strip()
        return None

    def _renforge_call_sites():
        """The return points of the running context's calls, innermost last."""
        try:
            stack = renpy.game.context().return_stack
            return builtins.tuple(builtins.repr(name) for name in stack)
        except Exception:
            return ()

    def _renforge_seen_current():
        """Whether the current statement already finished once since launch.

        Ren'Py marks a statement seen when it completes (seen_session), so a
        repeated line, menu, or hub reads true; loading a save does not reset it.
        """
        try:
            return bool(renpy.game.context().seen_current(False))
        except Exception:
            return None

    def _renforge_observe_stability():
        transition = _renforge_transition_pending()
        interacting = False
        try:
            interacting = bool(renpy.game.context().interacting)
        except Exception:
            interacting = False
        if transition:
            return False, "transition"
        bridge = _renforge_runtime.bridge
        if bridge is not None and getattr(bridge, "say_ended_generation", None) == bridge.observe_generation:
            return False, "not_ready"
        if not interacting or getattr(renpy.display.interface, "restart_interaction", False):
            return False, "not_ready"
        # Ren'Py sets interact_time on an interaction's first drawn frame and
        # clears it when the interaction ends. Before that frame the capture
        # and geometry still show the previous interaction.
        if getattr(renpy.display.interface, "interact_time", True) is None:
            return False, "not_ready"
        if _renforge_short_pause_pending():
            return False, "pause"
        return True, None


    def _renforge_engine_main_menu():
        """True at the engine's main menu, False in gameplay, None when unknown.

        Reads engine context flags, never game-defined screen or label names.
        """
        try:
            context = renpy.context()
            main_menu = getattr(context, "_main_menu", None)
            if not isinstance(main_menu, bool):
                main_menu = getattr(renpy.store, "main_menu", None)
        except Exception:
            main_menu = None
        return main_menu if isinstance(main_menu, bool) else None

    def _renforge_commit_ending():
        """Confirm a pending script return as the ending once the main menu is up.

        Called from the start-interaction hook, so the ``ending`` event fires
        once whether or not anything observes, and before the player can
        start a new game. Observe only reads the result.
        """
        bridge = _renforge_runtime.bridge
        if bridge is None or bridge.pending_ending is None or _renforge_engine_main_menu() is not True:
            return
        bridge.ending_counter += 1
        bridge.ending = dict(bridge.pending_ending, id=bridge.ending_counter)
        bridge.pending_ending = None
        bridge.push_event("ending", bridge.ending)

    def _renforge_game_lifecycle():
        """The game's phase (playing, main_menu, ended, unknown) and its ending; a pure read."""
        bridge = _renforge_runtime.bridge
        main_menu = _renforge_engine_main_menu()
        if main_menu is None:
            return {"phase": "unknown", "ending": None}
        ending = bridge.ending if main_menu and bridge is not None else None
        return {"phase": "ended" if ending else "main_menu" if main_menu else "playing",
                "ending": ending}

    def _renforge_input_candidates(inventory, pairs):
        """Report opaque event implementations without interpreting game code."""
        handled = {id(getattr(focus, "widget", None)) for focus, record in pairs if record.get("operations")}
        behavior = getattr(renpy.display, "behavior", None)
        known = tuple(cls for cls in (getattr(behavior, "Button", None),
            getattr(behavior, "Input", None), getattr(behavior, "Bar", None),
            getattr(getattr(renpy.display, "viewport", None), "Viewport", None),
            getattr(getattr(renpy.display, "dragdrop", None), "Drag", None)) if isinstance(cls, type))
        candidates = []
        unclassified = False
        for node, layer, name, screen, _parents in inventory["nodes"]:
            if id(node) in handled or (known and isinstance(node, known)):
                continue
            owner = next((cls for cls in type(node).__mro__ if "event" in vars(cls)), None)
            if owner is None or owner.__module__.startswith("renpy."):
                continue
            if not callable(vars(owner)["event"]):
                continue
            geometry = _renforge_widget_geometry(node, inventory)
            blocked = bool(_renforge_engine_modal_blocked(node, inventory))
            # A handler that draws at most one pixel cannot be pointed at: it
            # listens for keys, and dialogue still advances past it.
            bounds = geometry["bounds"] or {}
            pointer = not (geometry["visible"] is True and
                           bounds.get("width", 0) * bounds.get("height", 0) <= 1)
            relevant = geometry["visible"] is not False and not blocked and pointer
            unclassified = unclassified or relevant
            if len(candidates) >= _RENFORGE_OBSERVE_MAX_CANDIDATES:
                unclassified = True
                break
            if geometry["visible"] is False or not pointer:
                # Hidden or one-pixel handlers (gamepad and key listeners) have
                # nothing to point at, and dialogue advances past them.
                continue
            candidates.append({"screen": name, "layer": layer,
                "source": _renforge_source_location(node),
                "class": "%s.%s" % (type(node).__module__, type(node).__name__),
                "reason": "custom event handler; input semantics and eligibility unknown",
                "bounds": geometry["bounds"], "bounds_reason": geometry["bounds_reason"],
                "visible": geometry["visible"], "modal_blocked": blocked,
                "occurrences": inventory["geometry"]["occurrences"].get(id(node), [])})
        return candidates, unclassified


    def _renforge_observe_raw(screenshot, with_pairs=False, include_readout=True):
        """The bridge's observation of the current interaction, before classification.

        Reads stability, statement, label, lifecycle, dialogue, screens,
        controls, readout, input candidates and key bindings; changes no game
        state. ``with_pairs`` also returns the (focus, record) pairs that act
        and select_choice use.
        """
        bridge = _renforge_runtime.bridge
        interaction = 0
        if bridge is not None:
            interaction = int(getattr(bridge, "observe_generation", 0) or 0)
        stable, reason = _renforge_observe_stability()
        inventory = _renforge_scene_inventory()
        screens, say_dismiss, walk_truncated, screen_nodes = _renforge_collect_interaction(inventory)
        dialogue = _renforge_observe_dialogue(screen_nodes, say_dismiss)
        reveal_flags = [getattr(node.widgets.get("what"), "slow", None)
                        for node in screen_nodes
                        if isinstance(getattr(node, "widgets", None), builtins.dict) and "what" in node.widgets]
        text_revealing = any(flag is True for flag in reveal_flags) if reveal_flags else None
        pairs, focus_truncated = _renforge_observe_focus_pairs(inventory)
        candidates, unclassified = _renforge_input_candidates(inventory, pairs)
        elements = []
        blocked = set()
        for focus, record in pairs:
            copied = dict(record)
            copied.pop("center", None)
            elements.append(copied)
            widget = getattr(focus, "widget", None)
            # A viewport's text is content to read; controls inside it are
            # blocked on their own.
            if widget is not None and _renforge_observe_normalize_role(record.get("role")) != "viewport":
                blocked.add(id(widget))
        readout, readout_truncated = _renforge_observe_readout(blocked, inventory) if include_readout else ([], False)
        overlay = []
        try:
            for name in list(renpy.config.overlay_screens):
                if isinstance(name, str) and name:
                    overlay.append(name)
        except Exception:
            overlay = []
        statement = None
        try:
            filename, line = renpy.get_filename_line()
            file_name = filename if isinstance(filename, str) else None
            line_no = line if isinstance(line, builtins.int) and not isinstance(line, bool) else None
            if file_name is not None or line_no is not None:
                statement = {"file": file_name, "line": line_no}
        except Exception:
            statement = None
        # The label holding the statement that is running. The last label
        # entered goes stale when a call returns, and after a load whose
        # after_load label calls others.
        try:
            label = (_renforge_enclosing_label(statement["file"], statement["line"], falls_through=True)
                     if statement else None)
        except Exception:
            label = None
        if label is None and bridge is not None:
            label = bridge.current_label if isinstance(bridge.current_label, str) else None
        reply = {
            "ok": True,
            "interaction": interaction,
            "stable": stable,
            "unstable_reason": reason,
            "statement": statement,
            "label": label,
            "lifecycle": _renforge_game_lifecycle(),
            "dialogue": dialogue,
            "text_revealing": text_revealing,
            "screens": screens,
            "say_dismiss": say_dismiss,
            "overlay_screens": overlay,
            "elements": elements,
            "readout": readout,
            "input_candidates": candidates,
            "keys": _renforge_key_bindings(inventory),
            "omitted": {
                "focus_truncated": bool(focus_truncated),
                "unclassified": bool(walk_truncated or unclassified or inventory["geometry"]["truncated"]),
                "readout_truncated": readout_truncated,
            },
            "frame_hash": None,
            "event_seq": int(getattr(bridge, "event_seq", 0) or 0) if bridge is not None else 0,
        }
        reply["session_id"] = bridge.session_id if bridge is not None else None
        reply["say_id"] = getattr(bridge, "say_id", None)
        start = getattr(bridge, "start_label_state", None) if bridge is not None else None
        if start:
            # Kept for the session: what came before the label never ran.
            if start.get("error"):
                reply["start_label_error"] = "%s: %s" % (start["error"], start["label"])
            else:
                reply["started_at_label"] = start["label"]
        reply["seen"] = _renforge_seen_current()
        # Ren'Py's own error screen: the game raised an exception and waits
        # for Rollback, Ignore, or Quit.
        shown = {screen.get("name") for screen in screens}
        if "_exception" in shown:
            reply["game_error"] = builtins.dict(getattr(bridge, "last_exception", None) or {"message": None})
        elif "_parse_errors" in shown:
            # The scripts do not parse; Ren'Py lists why in errors.txt.
            reply["game_error"] = {"message": _renforge_first_parse_error(), "traceback_file": "errors.txt"}
        if bridge is not None and bridge.progression is not None:
            reply["progression"] = _renforge_progress_public(bridge.progression)
        identity = {
            "session_id": reply["session_id"], "interaction": interaction,
            "screens": screens, "elements": elements, "say_dismiss": say_dismiss,
            "input_candidates": candidates,
            "lifecycle": reply["lifecycle"],
            "widgets": [id(getattr(focus, "widget", None)) for focus, _record in pairs],
            "actions": [id(_renforge_focus_action_value(focus)) for focus, _record in pairs],
            "input_values": [getattr(getattr(focus, "widget", None), "content", None)
                             for focus, record in pairs if record.get("role") == "input"],
        }
        reply["observation_id"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, default=lambda value: None).encode("utf-8")
        ).hexdigest()
        if bridge is not None:
            # A few recent observations, so an action chosen from one stays
            # valid while its own control is unchanged (see act).
            recent = getattr(bridge, "recent_observations", None) or builtins.dict()
            recent.pop(reply["observation_id"], None)
            recent[reply["observation_id"]] = (reply["session_id"], interaction,
                                               _renforge_control_fingerprints(pairs))
            while len(recent) > 16:
                recent.pop(next(iter(recent)))
            bridge.recent_observations = recent
        if screenshot:
            try:
                data = renpy.screenshot_to_bytes(None)
                reply["screenshot_base64"] = base64.b64encode(data).decode("ascii")
                reply["sha256"] = hashlib.sha256(data).hexdigest()
                reply["frame_hash"] = reply["sha256"]
                width, height = _renforge_png_dimensions(data)
                reply["frame_id"] = reply["sha256"]
                reply["frame"] = {
                    "frame_id": reply["frame_id"], "session_id": reply["session_id"],
                    "width": width, "height": height,
                    "original_width": width, "original_height": height,
                    "logical_width": renpy.config.screen_width,
                    "logical_height": renpy.config.screen_height,
                }
            except Exception:
                reply["frame_hash"] = None
        return (reply, pairs) if with_pairs else reply


    _RENFORGE_EXPLAIN_TEXT_CHARS = 160
    _RENFORGE_EXPLAIN_KEYWORDS = ("action", "clicked", "sensitive", "hovered", "unhovered", "selected", "alternate", "value", "id")


    def _renforge_explain_str(value):
        if value is None:
            return ""
        if isinstance(value, builtins.str):
            return value
        return builtins.str(value)


    def _renforge_explain_text(value):
        text = " ".join(_renforge_explain_str(value).split())
        if len(text) > _RENFORGE_EXPLAIN_TEXT_CHARS:
            text = text[:_RENFORGE_EXPLAIN_TEXT_CHARS - 3] + "..."
        return text


    def _renforge_explain_location_tuple(location):
        """Normalize a ``(file, line)`` pair; None when the value is not one."""
        if not _renforge_observe_is_sequence(location) or len(location) < 2:
            return None
        try:
            filename = builtins.str(location[0])
            # Ren'Py 8.0 records compiled scripts under their .rpyc name; report the source name.
            if filename.endswith(".rpyc"):
                filename = filename[:-1]
            return (filename, builtins.int(location[1]))
        except (TypeError, ValueError):
            return None


    def _renforge_explain_loc(node):
        """``{"file", "line"}`` for script nodes and screen-language nodes."""
        pair = None
        filename = getattr(node, "filename", None)
        linenumber = getattr(node, "linenumber", None)
        if filename is not None and linenumber is not None:
            pair = _renforge_explain_location_tuple((filename, linenumber))
        if pair is None:
            pair = _renforge_explain_location_tuple(getattr(node, "location", None))
        if pair is None:
            return None
        return {"file": pair[0], "line": pair[1]}


    def _renforge_explain_pysrc(code):
        source = getattr(code, "source", None)
        if isinstance(source, builtins.str):
            return source
        return None


    def _renforge_explain_code_text(code):
        source = _renforge_explain_pysrc(code)
        if source is None:
            return ""
        for line in source.splitlines():
            if line.strip():
                return line.strip()
        return ""


    def _renforge_explain_name(raw):
        if isinstance(raw, (builtins.list, builtins.tuple)):
            return " ".join(_renforge_explain_str(part) for part in raw)
        return _renforge_explain_str(raw)


    def _renforge_explain_branch(index, cond, first):
        """Kind and condition text for entry ``index`` of an if-chain."""
        if index > 0 and cond in (None, "True"):
            return "else", ""
        return (first if index == 0 else "elif"), _renforge_explain_str(cond)


    def _renforge_explain_emit(state, depth, kind, loc, text):
        if len(state["entries"]) >= state["limit"]:
            state["truncated"] = True
            return
        where = loc or {}
        entry = {"depth": depth, "kind": kind, "line": where.get("line"), "text": _renforge_explain_text(text)}
        if where.get("file") != state.get("file"):
            # Entries name their file only when it differs from the result's own.
            entry["file"] = where.get("file")
        state["entries"].append(entry)


    def _renforge_explain_label_walk(block, depth, state):
        for node in block or ():
            if state["truncated"]:
                return
            try:
                _renforge_explain_label_node(node, depth, state)
            except Exception:
                continue


    def _renforge_explain_label_node(node, depth, state):
        kind = node.__class__.__name__
        loc = _renforge_explain_loc(node)
        emit = _renforge_explain_emit
        if kind in ("Say", "TranslateSay"):
            who = getattr(node, "who", None)
            what = getattr(node, "what", None)
            if who:
                text = "%s: %s" % (_renforge_explain_str(who), _renforge_explain_str(what))
            else:
                text = _renforge_explain_str(what)
            emit(state, depth, "say", loc, text)
        elif kind == "Menu":
            emit(state, depth, "menu", loc, "")
            for item in getattr(node, "items", None) or ():
                if state["truncated"]:
                    return
                caption, condition, block = item[0], item[1], item[2]
                if block is None:
                    emit(state, depth, "caption", loc, _renforge_explain_str(caption))
                    continue
                text = _renforge_explain_str(caption)
                if condition not in (None, "True"):
                    text = "%s if %s" % (text, _renforge_explain_str(condition))
                    _renforge_explain_note(state, condition=condition)
                emit(state, depth, "choice", loc, text)
                _renforge_explain_label_walk(block, depth + 1, state)
        elif kind == "If":
            for index, entry in enumerate(getattr(node, "entries", None) or ()):
                branch, cond_text = _renforge_explain_branch(index, entry[0], "if")
                _renforge_explain_note(state, condition=cond_text)
                emit(state, depth, branch, loc, cond_text)
                _renforge_explain_label_walk(entry[1], depth + 1, state)
        elif kind == "While":
            _renforge_explain_note(state, condition=getattr(node, "condition", None))
            emit(state, depth, "while", loc, getattr(node, "condition", None))
            _renforge_explain_label_walk(getattr(node, "block", None), depth + 1, state)
        elif kind in ("Jump", "Call"):
            target = getattr(node, "label", None) if kind == "Call" else getattr(node, "target", None)
            prefix = "expression " if getattr(node, "expression", None) else ""
            emit(state, depth, kind.lower(), loc, prefix + _renforge_explain_str(target))
        elif kind == "Return":
            emit(state, depth, "return", loc, getattr(node, "expression", None))
        elif kind in ("Python", "EarlyPython"):
            emit(state, depth, "python", loc, _renforge_explain_code_text(getattr(node, "code", None)))
        elif kind in ("Show", "Scene", "Hide"):
            imspec = getattr(node, "imspec", None)
            name = ""
            if imspec and imspec[0]:
                name = " ".join(_renforge_explain_str(part) for part in imspec[0])
            emit(state, depth, kind.lower(), loc, name)
        elif kind == "Translate":
            _renforge_explain_label_walk(getattr(node, "block", None), depth, state)
        elif kind in ("EndTranslate", "Pass"):
            return
        elif kind == "UserStatement":
            emit(state, depth, "statement", loc, getattr(node, "line", None))
        elif kind == "Label":
            emit(state, depth, "label", loc, getattr(node, "name", None))
        else:
            emit(state, depth, kind, loc, "")


    def _renforge_explain_namemap():
        script = getattr(getattr(renpy, "game", None), "script", None)
        namemap = getattr(script, "namemap", None)
        return namemap if namemap is not None else {}


    def _renforge_explain_screens():
        """``(name, variant, screen)`` for every defined screen."""
        screen_module = getattr(getattr(renpy, "display", None), "screen", None)
        screens = getattr(screen_module, "screens", None)
        if screens is None:
            return []
        found = []
        for key, screen in screens.items():
            try:
                name, variant = key[0], key[1]
            except (TypeError, IndexError):
                continue
            found.append((_renforge_explain_name(name), variant, screen))
        return found


    def _renforge_explain_sort_key(item):
        return (item.get("file") or "", item.get("line") or 0, item.get("name") or "")


    def _renforge_explain_index(query=None, limit=200):
        """Labels and screens, optionally filtered by a case-insensitive name substring."""
        needle = query.casefold() if query else None
        labels = []
        for key, value in _renforge_explain_namemap().items():
            # Ren'Py keys the namemap by the node itself, which hashes as its name.
            name = key if isinstance(key, builtins.str) else getattr(value, "name", None)
            if not isinstance(name, builtins.str) or name.startswith("_"):
                continue
            if value.__class__.__name__ != "Label":
                continue
            if needle and needle not in name.casefold():
                continue
            where = _renforge_explain_loc(value) or {}
            labels.append({"name": name, "file": where.get("file"), "line": where.get("line")})
        labels.sort(key=_renforge_explain_sort_key)
        label_total = len(labels)

        layers = list(getattr(renpy.config, "layers", None) or ()) + list(getattr(renpy.config, "top_layers", None) or ())
        screens = []
        for name, variant, screen in _renforge_explain_screens():
            if name.startswith("_") or (needle and needle not in name.casefold()):
                continue
            where = _renforge_explain_loc(screen) or {}
            params = getattr(screen, "parameters", None)
            shown_on = []
            for layer in layers:
                try:
                    if renpy.get_screen(name, layer=layer) is not None:
                        shown_on.append(layer)
                except Exception:
                    continue
            screens.append({
                "name": name,
                "variant": variant,
                "file": where.get("file"),
                "line": where.get("line"),
                "parameters": _renforge_explain_parameters(params) if params else None,
                "shown_on": shown_on,
            })
        screens.sort(key=_renforge_explain_sort_key)

        files = {}
        for item in labels + screens:
            if item.get("file") is not None:
                files[item["file"]] = True
        base = getattr(renpy.config, "basedir", "") or ""
        on_disk = sum(1 for name in files if os.path.exists(os.path.join(base, name)))
        screen_total = len(screens)
        return {
            "ok": True,
            "query": query,
            "labels": labels[:limit],
            "label_total": label_total,
            "screens": screens[:limit],
            "screen_total": screen_total,
            "truncated": label_total > limit or screen_total > limit,
            "files": {"total": len(files), "on_disk": on_disk},
        }


    def _renforge_explain_label(name, limit):
        node = _renforge_explain_namemap().get(name)
        if node is None or node.__class__.__name__ != "Label":
            return {"ok": False, "error": "unknown label"}
        where = _renforge_explain_loc(node) or {}
        state = {"entries": [], "limit": limit, "truncated": False, "file": where.get("file")}
        block = getattr(node, "block", None)
        fell_into = []
        hops = 0
        while not block and hops < 50:
            # Ren'Py runs on past a label with no block: into the statements
            # after it, or into the next label's own block.
            hops += 1
            follow = getattr(node, "next", None)
            if follow is None:
                break
            if follow.__class__.__name__ == "Label":
                node = follow
                fell_into.append(getattr(follow, "name", None))
                block = getattr(follow, "block", None)
                continue
            block = []
            while follow is not None and follow.__class__.__name__ != "Label" and len(block) < 2000:
                block.append(follow)
                follow = getattr(follow, "next", None)
        _renforge_explain_label_walk(block, 0, state)
        return {
            "ok": True,
            "label": name,
            "file": where.get("file"),
            "line": where.get("line"),
            "statements": state["entries"],
            "truncated": state["truncated"],
            **({"falls_into": fell_into} if fell_into else {}),
            **_renforge_explain_noted(state),
        }


    def _renforge_explain_sl_text(node):
        parts = [_renforge_explain_str(item) for item in getattr(node, "positional", None) or ()]
        text = " ".join(parts)
        for pair in getattr(node, "keyword", None) or ():
            key = _renforge_explain_str(pair[0])
            if key in _RENFORGE_EXPLAIN_KEYWORDS:
                text += " %s %s" % (key, _renforge_explain_str(pair[1]))
        return text.strip()


    def _renforge_explain_screen_walk(nodes, depth, state):
        for node in nodes or ():
            if state["truncated"]:
                return
            try:
                _renforge_explain_screen_node(node, depth, state)
            except Exception:
                continue


    def _renforge_explain_screen_node(node, depth, state):
        kind = node.__class__.__name__
        loc = _renforge_explain_loc(node)
        emit = _renforge_explain_emit
        children = getattr(node, "children", None)
        if kind == "SLDisplayable":
            name = getattr(node, "name", None)
            if not isinstance(name, builtins.str):
                name = getattr(name, "__name__", None) or _renforge_explain_str(name)
            emit(state, depth, name, loc, _renforge_explain_sl_text(node))
            _renforge_explain_screen_walk(children, depth + 1, state)
        elif kind in ("SLIf", "SLShowIf"):
            first = "showif" if kind == "SLShowIf" else "if"
            for index, entry in enumerate(getattr(node, "entries", None) or ()):
                branch, cond_text = _renforge_explain_branch(index, entry[0], first)
                _renforge_explain_note(state, condition=cond_text)
                # A branch can set its parent's properties, such as a button's action.
                keywords = _renforge_explain_sl_text(types.SimpleNamespace(
                    keyword=getattr(entry[1], "keyword", None)))
                emit(state, depth, branch, loc, (cond_text + " : " + keywords) if keywords else cond_text)
                _renforge_explain_screen_walk(getattr(entry[1], "children", None), depth + 1, state)
        elif kind == "SLFor":
            text = "%s in %s" % (_renforge_explain_str(getattr(node, "variable", None)),
                                 _renforge_explain_str(getattr(node, "expression", None)))
            _renforge_explain_note(state, binds=getattr(node, "variable", None))
            emit(state, depth, "for", loc, text)
            _renforge_explain_screen_walk(children, depth + 1, state)
        elif kind == "SLUse":
            emit(state, depth, "use", loc, _renforge_explain_str(getattr(node, "target", None)))
            _renforge_explain_screen_walk(getattr(getattr(node, "block", None), "children", None), depth + 1, state)
        elif kind == "SLPython":
            emit(state, depth, "python", loc, _renforge_explain_code_text(getattr(node, "code", None)))
        elif kind == "SLDefault":
            text = "%s = %s" % (_renforge_explain_str(getattr(node, "variable", None)),
                                _renforge_explain_str(getattr(node, "expression", None)))
            emit(state, depth, "default", loc, text)
        elif kind == "SLBlock" or children is not None:
            _renforge_explain_screen_walk(children, depth, state)
        else:
            emit(state, depth, kind, loc, "")


    def _renforge_explain_screen(name, limit):
        match = None
        for screen_name, variant, screen in _renforge_explain_screens():
            if screen_name != name:
                continue
            if match is None or (variant is None and match[1] is not None):
                match = (screen_name, variant, screen)
        if match is None:
            return {"ok": False, "error": "unknown screen"}
        screen_name, variant, screen = match
        where = _renforge_explain_loc(screen) or {}
        head = {
            "ok": True,
            "screen": screen_name,
            "variant": variant,
            "file": where.get("file"),
            "line": where.get("line"),
            "parameters": _renforge_explain_parameters(getattr(screen, "parameters", None)) or None,
        }
        ast = getattr(screen, "ast", None)
        if ast is None:
            head.update({"nodes": [], "truncated": False, "reason": "screen defined in Python"})
            return head
        state = {"entries": [], "limit": limit, "truncated": False, "file": where.get("file")}
        _renforge_explain_screen_walk(getattr(ast, "children", None), 0, state)
        head.update({"nodes": state["entries"], "truncated": state["truncated"]})
        head.update(_renforge_explain_noted(state))
        return head


    def _renforge_explain_branches(node):
        """``(label, children)`` pairs a screen-language node descends into."""
        kind = node.__class__.__name__
        if kind in ("SLIf", "SLShowIf"):
            branches = []
            for index, entry in enumerate(getattr(node, "entries", None) or ()):
                branch, cond_text = _renforge_explain_branch(index, entry[0], "if")
                label = "%s %s" % (branch, cond_text) if cond_text else branch
                branches.append((label, getattr(entry[1], "children", None)))
            return branches
        if kind == "SLFor":
            label = "for %s in %s" % (_renforge_explain_str(getattr(node, "variable", None)),
                                      _renforge_explain_str(getattr(node, "expression", None)))
            return [(label, getattr(node, "children", None))]
        if kind == "SLUse":
            label = "use %s" % _renforge_explain_str(getattr(node, "target", None))
            return [(label, getattr(getattr(node, "block", None), "children", None))]
        children = getattr(node, "children", None)
        return [(None, children)] if children is not None else []


    def _renforge_explain_find(nodes, wanted, path):
        """Depth-first search for the SLDisplayable at ``wanted``; returns (node, conditions)."""
        for node in nodes or ():
            try:
                if (node.__class__.__name__ == "SLDisplayable"
                        and _renforge_explain_location_tuple(getattr(node, "location", None)) == wanted):
                    return node, path
                for label, children in _renforge_explain_branches(node):
                    next_path = path + [label] if label else path
                    found = _renforge_explain_find(children, wanted, next_path)
                    if found is not None:
                        return found
            except Exception:
                continue
        return None


    def _renforge_explain_statement(wanted):
        """The screen-language statement at ``(file, line)``, with its enclosing conditions."""
        seen = set()
        for screen_name, _variant, screen in _renforge_explain_screens():
            ast = getattr(screen, "ast", None)
            if ast is None or id(ast) in seen:
                continue
            seen.add(id(ast))
            found = _renforge_explain_find(getattr(ast, "children", None), wanted, [])
            if found is None:
                continue
            node, conditions = found
            where = _renforge_explain_loc(node) or {}
            return {
                "defined_in_screen": screen_name,
                "statement": {
                    "kind": _renforge_explain_name(getattr(node, "name", None)),
                    "file": where.get("file"),
                    "line": where.get("line"),
                    "positional": [_renforge_explain_text(item) for item in getattr(node, "positional", None) or ()],
                    "keywords": {
                        _renforge_explain_str(pair[0]): _renforge_explain_text(pair[1])
                        for pair in getattr(node, "keyword", None) or ()
                    },
                },
                "conditions": list(conditions),
            }
        return {"statement": None, "reason": "location not found in screen definitions"}


    def _renforge_h_explain_control(control_id):
        focus, element, error = _renforge_resolve_ui_element({"id": control_id}, "explain")
        if error:
            return error
        widget = getattr(focus, "widget", None)
        # Every fact observe knows about the control; observe itself is compact.
        summary = {key: value for key, value in element.items() if not key.startswith("_")}
        if element.get("control_id"):
            summary["id"] = element["control_id"]
        wanted = _renforge_source_tuple(widget) if widget is not None else None
        if wanted is None:
            return {
                "ok": True,
                "control": summary,
                "statement": None,
                "reason": "built in Python; no screen-language location",
            }
        return dict(_renforge_explain_statement(wanted), ok=True, control=summary)


    def _renforge_h_explain_source(source):
        """Explain a ``file:line`` reported as ``source`` by observe or input candidates."""
        filename, _sep, line = source.rpartition(":")
        try:
            wanted = _renforge_explain_location_tuple((filename, builtins.int(line)))
        except ValueError:
            wanted = None
        if not filename or wanted is None:
            return {"ok": False, "error": "source must look like file:line"}
        found = _renforge_explain_statement(wanted)
        if found.get("statement") is None:
            script = _renforge_explain_script_statement(wanted)
            if script is not None:
                return dict(script, ok=True, source=source)
        return dict(found, ok=True, source=source)


    def _renforge_explain_script_statement(wanted):
        """The script statements at ``(file, line)``, with their full text."""
        script = getattr(getattr(renpy, "game", None), "script", None)
        found = []
        noted = {"_conditions": [], "_bound": []}
        for node in getattr(script, "all_stmts", None) or ():
            where = _renforge_explain_loc(node)
            if where is None or (where["file"], where["line"]) != wanted:
                continue
            texts = _renforge_explain_node_texts(node)
            entry = {"kind": node.__class__.__name__,
                     "text": "\n".join(text for _kind, text in texts)[:4000]}
            if entry["kind"] in ("Menu", "If", "While"):
                # What each branch does, as explain(label) would list it.
                state = {"entries": [], "limit": 300, "truncated": False, "file": where["file"]}
                _renforge_explain_label_walk([node], 0, state)
                entry["statements"] = state["entries"]
                for key, items in _renforge_explain_noted(state).items():
                    noted[key].extend(items)
            found.append(entry)
            if len(found) >= 8:
                break
        if not found:
            return None
        return {"statement": None, "script": found, **noted}


    def _renforge_explain_node_texts(node):
        """Searchable text a script statement carries: dialogue, Python, conditions, captions."""
        kind = node.__class__.__name__
        texts = []
        what = getattr(node, "what", None)
        if isinstance(what, builtins.str):
            texts.append(("say", what))
        source = _renforge_explain_pysrc(getattr(node, "code", None))
        if source:
            texts.append(("python", source))
        if kind == "If":
            for entry in getattr(node, "entries", None) or ():
                if isinstance(entry[0], builtins.str):
                    texts.append(("if", entry[0]))
        elif kind == "Menu":
            for item in getattr(node, "items", None) or ():
                caption = item[0] if isinstance(item[0], builtins.str) else ""
                condition = item[1] if isinstance(item[1], builtins.str) else ""
                texts.append(("choice", "%s if %s" % (caption, condition) if condition not in ("", "True") else caption))
        elif kind in ("Jump", "Call"):
            target = getattr(node, "target", None) if kind == "Jump" else getattr(node, "label", None)
            if isinstance(target, builtins.str):
                texts.append((kind.lower(), target))
        return texts


    def _renforge_label_last_line(label, first):
        """The last line of a label's block, from Ren'Py's own walk of it."""
        last = [first]

        def visit(node):
            line = getattr(node, "linenumber", None)
            if isinstance(line, builtins.int) and not isinstance(line, bool) and line > last[0]:
                last[0] = line

        def walk(node):
            # Without get_children: a block, and the blocks of if/menu entries.
            visit(node)
            for child in getattr(node, "block", None) or ():
                walk(child)
            for entry in list(getattr(node, "entries", None) or ()) + list(getattr(node, "items", None) or ()):
                if _renforge_observe_is_sequence(entry) and entry and _renforge_observe_is_sequence(entry[-1]):
                    for child in entry[-1]:
                        walk(child)

        try:
            label.get_children(visit)
        except Exception:
            walk(label)
        return last[0]


    def _renforge_label_lines():
        """``{file: [(first, last, label), ...]}`` by first line, rebuilt when the script reloads.

        Ren'Py's own return-point labels (``_call_<label>_<n>``) are left out:
        they name a call site, not the label a player is in.
        """
        script = getattr(getattr(renpy, "game", None), "script", None)
        namemap = _renforge_explain_namemap()
        key = (id(script), len(namemap))
        # Kept on the runtime module, outside the store, so saves and
        # rollback never carry it.
        cache = getattr(_renforge_runtime, "label_lines", None)
        if cache is not None and cache[0] == key:
            return cache[1]
        labels = builtins.dict()
        for name_key, value in list(namemap.items()):
            if value.__class__.__name__ != "Label":
                continue
            name = name_key if isinstance(name_key, builtins.str) else getattr(value, "name", None)
            if not isinstance(name, builtins.str) or name.startswith("_call_"):
                continue
            where = _renforge_explain_loc(value)
            if where:
                labels.setdefault(where["file"], builtins.list()).append(
                    (where["line"], _renforge_label_last_line(value, where["line"]), name))
        for entries in labels.values():
            entries.sort()
        _renforge_runtime.label_lines = (key, labels)
        return labels


    def _renforge_enclosing_label(file_name, line, falls_through=False):
        """The innermost label whose block holds ``file_name:line``, or None.

        A top-level define, screen, or init block after a label is in no label.
        A story statement outside every label block (``label start:`` with an
        empty block, the story written unindented below it) is reached by
        falling through the label before it; ``falls_through`` names that
        label.
        """
        if not isinstance(file_name, builtins.str) or not isinstance(line, builtins.int):
            return None
        if file_name.endswith(".rpyc"):
            file_name = file_name[:-1]
        enclosing = None
        preceding = None
        for first, last, name in _renforge_label_lines().get(file_name, ()):
            if first > line:
                break
            preceding = name
            if line <= last:
                enclosing = name
        if enclosing is None and falls_through:
            return preceding
        return enclosing


    # Statements that only run in story flow, never at init time.
    _RENFORGE_STORY_STATEMENTS = frozenset((
        "Say", "Menu", "Scene", "Show", "Hide", "With", "Jump", "Call", "Return",
        "Pass", "UserStatement", "If", "While",
    ))


    def _renforge_explain_parameters(params):
        """A screen's parameters as written: ``(items, title="")``.

        Ren'Py 8.3's Signature prints itself; 8.0-8.2's ParameterInfo keeps
        (name, default) pairs and the *args/**kwargs names.
        """
        if params is None:
            return None
        text = _renforge_explain_str(params)
        if text and not text.startswith("<"):
            return text
        pairs = getattr(params, "parameters", None)
        if not _renforge_observe_is_sequence(pairs):
            return text
        parts = []
        for pair in pairs:
            if _renforge_observe_is_sequence(pair) and len(pair) == 2:
                name, default = pair
                parts.append(builtins.str(name) if default is None else "%s=%s" % (name, default))
        if getattr(params, "extrapos", None):
            parts.append("*" + builtins.str(params.extrapos))
        if getattr(params, "extrakw", None):
            parts.append("**" + builtins.str(params.extrakw))
        return "(" + ", ".join(parts) + ")"


    def _renforge_explain_snippet(text, needle):
        """Up to 60 characters either side of the first case-insensitive ``needle``.

        The window is found in ``text`` itself: an index into its casefold
        can point past the match, since folding changes length (sharp s
        folds to "ss"). A match only folding finds shows the start of the text."""
        import re as _re
        found = _re.search(_re.escape(needle), text, _re.IGNORECASE)
        start, end = (found.start(), found.end()) if found else (0, len(needle))
        return text[max(0, start - 60):end + 60]


    def _renforge_explain_search(needle, limit):
        """Every loaded statement whose text contains ``needle``, with its enclosing label."""
        wanted = needle.casefold()
        script = getattr(getattr(renpy, "game", None), "script", None)
        matches = []
        truncated = False
        for node in getattr(script, "all_stmts", None) or ():
            try:
                found = [(kind, text) for kind, text in _renforge_explain_node_texts(node)
                         if wanted in text.casefold()]
            except Exception:
                continue
            if not found:
                continue
            if len(matches) >= limit:
                truncated = True
                break
            where = _renforge_explain_loc(node) or {}
            enclosing = _renforge_enclosing_label(
                where.get("file"), where.get("line") or 0,
                falls_through=node.__class__.__name__ in _RENFORGE_STORY_STATEMENTS)
            kind, text = found[0]
            matches.append({"kind": kind, "file": where.get("file"), "line": where.get("line"),
                            "label": enclosing, "text": _renforge_explain_text(_renforge_explain_snippet(text, needle))})
        # Screen language: displayable arguments, properties, and conditions.
        seen = set()
        for screen_name, _variant, screen in _renforge_explain_screens():
            ast = getattr(screen, "ast", None)
            if truncated or ast is None or id(ast) in seen:
                continue
            seen.add(id(ast))
            # Depth first in document order: children go on reversed, so the
            # first is popped first, and a cut keeps the earliest matches.
            stack = builtins.list(getattr(ast, "children", None) or ())[::-1]
            while stack and not truncated:
                node = stack.pop()
                try:
                    kind = node.__class__.__name__
                    texts = []
                    if kind == "SLDisplayable":
                        texts.append(_renforge_explain_sl_text(types.SimpleNamespace(
                            positional=getattr(node, "positional", None),
                            keyword=getattr(node, "keyword", None) or ())))
                        texts.extend(_renforge_explain_str(pair[1]) for pair in getattr(node, "keyword", None) or ())
                    elif kind in ("SLIf", "SLShowIf"):
                        for entry in getattr(node, "entries", None) or ():
                            texts.append(_renforge_explain_str(entry[0]))
                            texts.extend(_renforge_explain_str(pair[1]) for pair in getattr(entry[1], "keyword", None) or ())
                    elif kind == "SLPython":
                        texts.append(_renforge_explain_pysrc(getattr(node, "code", None)) or "")
                    hit = next((text for text in texts if wanted in text.casefold()), None)
                    below = []
                    for _label, children in _renforge_explain_branches(node):
                        below.extend(children or ())
                    stack.extend(below[::-1])
                except Exception:
                    continue
                if hit is None:
                    continue
                if len(matches) >= limit:
                    truncated = True
                    break
                where = _renforge_explain_loc(node) or {}
                matches.append({"kind": "screen", "file": where.get("file"), "line": where.get("line"),
                                "screen": screen_name,
                                "text": _renforge_explain_text(_renforge_explain_snippet(hit, needle))})
        return {"ok": True, "search": needle, "matches": matches, "truncated": truncated}


    _RENFORGE_VARIABLE_RE = r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*"
    _RENFORGE_VALUE_FIELDS = 40

    def _renforge_explain_value(value, depth=0):
        """A store value as data: plain values as they are, a game object as its
        simple public fields. Nothing is called, so no game code runs."""
        import enum as _enum
        import itertools as _itertools
        if isinstance(value, _enum.Enum):
            return "%s.%s" % (type(value).__name__, value.name)
        if value is None or isinstance(value, (bool, builtins.int, builtins.float, builtins.str)):
            return value
        if isinstance(value, (builtins.list, builtins.tuple, builtins.set, builtins.frozenset)):
            # Only the first entries are listed, so only they are taken.
            items = builtins.list(_itertools.islice(value, _RENFORGE_VALUE_FIELDS))
            return [_renforge_explain_value(item, depth + 1) for item in items] if depth < 2 else _renforge_jsonable(items)
        if isinstance(value, builtins.dict):
            items = builtins.list(_itertools.islice(value.items(), _RENFORGE_VALUE_FIELDS))
            return {str(k): (_renforge_explain_value(v, depth + 1) if depth < 2 else _renforge_jsonable(v)) for k, v in items}
        fields = getattr(value, "__dict__", None)
        if isinstance(fields, builtins.dict) and depth < 1:
            simple = {}
            for key, item in fields.items():
                if not isinstance(key, builtins.str) or key.startswith("_") or callable(item):
                    continue
                if len(simple) >= _RENFORGE_VALUE_FIELDS:
                    break
                simple[key] = _renforge_explain_value(item, depth + 1)
            return {"type": _renforge_type_label(value), "fields": simple}
        return "<%s>" % _renforge_type_label(value)

    def _renforge_explain_variable(path):
        """Read one store variable or dotted attribute path, never calling anything.

        The walk stays in game data: it never steps into a module (renpy is in
        the store, and renpy.os.environ is the environment the game inherited)
        or reads a private (``_``) attribute after the first name, matching
        the public fields _renforge_explain_value lists. Dict keys are data
        and may start with ``_``."""
        import re as _re
        if not isinstance(path, builtins.str) or not _re.fullmatch(_RENFORGE_VARIABLE_RE, path.strip()):
            return {"error": "not a variable name"}
        parts = path.strip().split(".")
        store = vars(renpy.store)
        if parts[0] not in store:
            return {"error": "not defined"}
        value = store[parts[0]]
        for part in parts[1:]:
            if isinstance(value, types.ModuleType):
                return {"error": "not data (%s)" % _renforge_type_label(value)}
            fields = getattr(value, "__dict__", None)
            if isinstance(value, builtins.dict) and part in value:
                value = value[part]
            elif part.startswith("_"):
                return {"error": "no field %s" % part}
            elif isinstance(fields, builtins.dict) and part in fields:
                value = fields[part]
            else:
                # A plain class attribute (a default the instance never set);
                # properties and methods are never read, since that runs code.
                missing = found = builtins.object()  # a class default can itself be None
                for cls in getattr(type(value), "__mro__", ()):
                    if part in vars(cls):
                        found = vars(cls)[part]
                        break
                if found is missing or isinstance(found, (property, builtins.staticmethod, builtins.classmethod)) \
                        or callable(found) or hasattr(found, "__get__"):
                    return {"error": "no field %s" % part}
                value = found
        if callable(value) or isinstance(value, types.ModuleType):
            return {"error": "not data (%s)" % _renforge_type_label(value)}
        result = {"value": _renforge_explain_value(value)}
        if isinstance(value, (builtins.list, builtins.tuple, builtins.set, builtins.frozenset, builtins.dict)) \
                and len(value) > _RENFORGE_VALUE_FIELDS:
            # Only the first entries are listed; read others by path (ran.some_key).
            result.update({"size": len(value), "truncated": True})
        return result

    def _renforge_condition_names(text):
        """Variables a condition reads (names and dotted paths, not calls)."""
        import ast as _ast
        source = (text or "").strip()
        for prefix in ("if ", "elif ", "while ", "showif "):
            if source.startswith(prefix):
                source = source[len(prefix):]
        try:
            tree = _ast.parse(source, mode="eval")
        except SyntaxError:
            return []
        called = builtins.set(id(node.func) for node in _ast.walk(tree) if isinstance(node, _ast.Call))
        # The start of a dotted path (persistent in persistent.x) is not read
        # on its own; only the whole path is.
        inner = builtins.set(id(node.value) for node in _ast.walk(tree) if isinstance(node, _ast.Attribute))
        local = _renforge_condition_locals(tree)
        names = []
        for node in _ast.walk(tree):
            if id(node) in called or id(node) in inner or not isinstance(node, (_ast.Name, _ast.Attribute)):
                continue
            parts, current = [], node
            while isinstance(current, _ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, _ast.Name) and id(current) not in local:
                names.append(".".join([current.id] + parts[::-1]))
        return names

    def _renforge_condition_locals(tree):
        """ids of the Name nodes a comprehension or lambda inside ``tree`` binds.

        ``q`` in ``any(q.done for q in quests)`` is the comprehension's own,
        not a store variable. A comprehension's first iterable is read in
        the enclosing scope, so a name there stays a store variable."""
        import ast as _ast
        local = builtins.set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Lambda):
                args = node.args
                params = (builtins.list(getattr(args, "posonlyargs", None) or ()) + builtins.list(args.args)
                          + builtins.list(args.kwonlyargs) + [args.vararg, args.kwarg])
                bound = builtins.set(param.arg for param in params if param is not None)
                scope = [node.body]
            elif isinstance(node, (_ast.ListComp, _ast.SetComp, _ast.GeneratorExp, _ast.DictComp)):
                bound = builtins.set(name.id for generator in node.generators
                                     for name in _ast.walk(generator.target) if isinstance(name, _ast.Name))
                scope = [getattr(node, field) for field in ("elt", "key", "value") if hasattr(node, field)]
                for index, generator in enumerate(node.generators):
                    scope.append(generator.target)
                    scope.extend(generator.ifs)
                    if index:
                        scope.append(generator.iter)
            else:
                continue
            for part in scope:
                for name in _ast.walk(part):
                    if isinstance(name, _ast.Name) and name.id in bound:
                        local.add(id(name))
        return local

    def _renforge_loop_targets(target):
        """Names a ``for`` target binds: ``i``, ``(key, value)``, ``a, (b, c)``."""
        import re as _re
        return builtins.set(_re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _renforge_explain_str(target)))

    def _renforge_explain_note(state, condition=None, binds=None):
        """Record a walked condition, or a loop target, for explain's ``values``.

        Entry text is for reading: it is shortened and can carry the
        properties a branch sets after its condition, so ``values`` reads
        the raw condition recorded here instead."""
        if condition:
            state.setdefault("conditions", []).append(_renforge_explain_str(condition))
        if binds:
            state.setdefault("bound", builtins.set()).update(_renforge_loop_targets(binds))

    def _renforge_explain_noted(state):
        """What a walk recorded with ``_renforge_explain_note``, under the
        private result keys ``_renforge_h_explain`` reads and then removes."""
        return {"_conditions": builtins.list(state.get("conditions") or ()),
                "_bound": builtins.sorted(state.get("bound") or ())}

    def _renforge_explain_condition_values(result):
        """Current values of the variables behind the conditions in a result.

        ``conditions`` are the conditions enclosing an explained control
        (``"if x > 3"``, ``"for i in items"``); ``_conditions`` and ``_bound``
        are the raw conditions and loop targets a label, screen or script
        walk recorded."""
        texts = []
        # Names a for loop binds are the loop's own, not store variables.
        bound = builtins.set(result.get("_bound") or ())
        for text in result.get("conditions") or ():
            if text.startswith("for "):
                bound.update(_renforge_loop_targets(text[4:].split(" in ", 1)[0]))
            else:
                texts.append(text)
        texts.extend(result.get("_conditions") or ())
        values = {}
        for text in texts:
            for name in _renforge_condition_names(text):
                if name in values or len(values) >= _RENFORGE_VALUE_FIELDS or name.split(".")[0] in bound:
                    continue
                read = _renforge_explain_variable(name)
                if "value" in read:
                    values[name] = read["value"]
        return values

    _RENFORGE_EXPLAIN_VARIABLES_MAX = 100

    def _renforge_h_explain(payload):
        """Explain game content from the loaded syntax trees, never evaluating it.

        Payload: ``variables`` (a list of store names or dotted fields, read
        as data; pass it alone) or one selector handled by
        ``_renforge_h_explain_selected``. Label, screen, control and source
        results also carry ``values``, the current values of the plain
        variables their conditions read. Errors: ``variables must be a list
        of names``, ``pass variables alone, ...`` and the selector errors."""
        payload = payload or {}
        if payload.get("variables") is not None:
            names = payload["variables"]
            if isinstance(names, builtins.str) or not isinstance(names, (builtins.list, builtins.tuple)) or not names:
                return {"ok": False, "error": "variables must be a list of names"}
            if any(payload.get(key) is not None for key in ("label", "screen", "id", "source", "search", "query")):
                return {"ok": False,
                        "error": "pass variables alone, without label, screen, id, source, search or query"}
            wanted = names[:_RENFORGE_EXPLAIN_VARIABLES_MAX]
            return {"ok": True, "variables": {str(name): _renforge_explain_variable(name) for name in wanted},
                    "truncated": len(names) > len(wanted)}
        result = _renforge_h_explain_selected(payload)
        if isinstance(result, builtins.dict) and result.get("ok") is not False:
            values = _renforge_explain_condition_values(result)
            if values:
                # What the conditions above read right now.
                result["values"] = values
        if isinstance(result, builtins.dict):
            # Walk notes feed values only; they are not part of the reply.
            result.pop("_conditions", None)
            result.pop("_bound", None)
        return result

    def _renforge_h_explain_selected(payload):
        """Answer explain for at most one selector.

        ``label`` (its statements), ``screen`` (its displayable tree), ``id``
        (a control's screen-language statement and enclosing conditions),
        ``source`` (``file:line``) or ``search`` (text in every loaded
        statement); none gives the label and screen index, filtered by
        ``query``. ``limit`` bounds entries (1-2000, default 200). Errors:
        ``pass at most one of ...``, ``<key> must be a string``, ``unknown
        label``/``unknown screen`` and ``search must not be empty``."""
        selectors = [key for key in ("label", "screen", "id", "source", "search") if payload.get(key) is not None]
        if len(selectors) > 1:
            return {"ok": False, "error": "pass at most one of label, screen, id, source, search"}
        for key in selectors:
            if not isinstance(payload.get(key), builtins.str):
                return {"ok": False, "error": "%s must be a string" % key}
        try:
            limit = builtins.int(payload.get("limit", 200))
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(2000, limit))
        query = payload.get("query")
        if query is not None and not isinstance(query, builtins.str):
            return {"ok": False, "error": "query must be a string"}
        if not selectors:
            return _renforge_explain_index(query, limit)
        if selectors[0] == "label":
            return _renforge_explain_label(payload["label"], limit)
        if selectors[0] == "screen":
            return _renforge_explain_screen(payload["screen"], limit)
        if selectors[0] == "source":
            return _renforge_h_explain_source(payload["source"])
        if selectors[0] == "search":
            if not payload["search"].strip():
                return {"ok": False, "error": "search must not be empty"}
            return _renforge_explain_search(payload["search"], limit)
        return _renforge_h_explain_control(payload["id"])



    def _renforge_h_observe(payload):
        payload = payload or {}
        return _renforge_observe_raw(bool(payload.get("screenshot")))


    def _renforge_h_dismiss_if(payload):
        """Queue ``dismiss`` only when ``guard_dismiss`` accepts a fresh frame.

        ``past_optional_controls`` and ``allow_pause`` opt in only when they are
        literally true; ``allow_pause`` lets a line held by a pending pause be
        clicked once (the stuck-pause nudge)."""
        payload = payload or {}
        raw = _renforge_observe_raw(False, include_readout=False)
        snapshot = _renforge_runtime.interaction.classify(raw)
        refusal = _renforge_runtime.interaction.guard_dismiss(
            snapshot, payload.get("interaction"), session_id=payload.get("session_id"),
            observation_id=payload.get("observation_id"),
            past_optional_controls=payload.get("past_optional_controls") is True,
            allow_pause=payload.get("allow_pause") is True)
        if refusal:
            return {"ok": False, "error": refusal, "interaction": raw["interaction"]}
        renpy.exports.queue_event("dismiss")
        return {"ok": True, "interaction": raw["interaction"]}

    def _renforge_progress_public(operation):
        return {key: operation[key] for key in ("id", "active", "stop", "steps")}

    _RENFORGE_PROGRESS_PERIODIC_MS = 5
    _RENFORGE_PROGRESS_RECLICK_S = 0.3


    def _renforge_periodic_interval(ms=None):
        """Set the engine's periodic timer; None restores its own interval."""
        core = getattr(renpy.display, "core", None)
        event = getattr(core, "PERIODIC", None)
        timer = getattr(getattr(pygame, "time", None), "set_timer", None)
        if event is None or not callable(timer):
            return
        try:
            timer(event, int(ms if ms is not None else getattr(core, "PERIODIC_INTERVAL", 50)))
        except Exception:
            pass


    def _renforge_progress_finish(reason):
        """End the active progression with stop ``reason``, restoring the skip
        state, skip delay, periodic timer and preferences it changed."""
        bridge = _renforge_runtime.bridge
        operation = bridge.progression if bridge is not None else None
        if operation is None or not operation["active"]:
            return
        operation["active"] = False
        operation["stop"] = reason
        _renforge_periodic_interval(None)
        # Owned skip never leaks beyond the operation. Native fast mode can
        # bypass interaction checks, so only native slow skip is used here.
        renpy.config.skipping = None
        # The operation's skipping was its own, not the player's: the skip
        # watcher reports no skip.stopped for it.
        bridge.prev_skipping = None
        if operation.get("skip_delay") is not None:
            renpy.config.skip_delay = operation["skip_delay"]
        prefs = operation["preferences"]
        for name, value in operation["saved_preferences"].items():
            setattr(prefs, name, value)

    def _renforge_progress_tick():
        """Advance the active progression by one check; runs on every drain.

        Stops it (``timeout``, ``max_steps``, ``loop``, ``unsupported`` or a
        decision boundary from the interaction contract) or lets the engine
        skip the current line with native slow skip, clicking instead when
        the game has turned skipping off. Pauses and transitions are skipped
        without judging the screens behind them."""
        bridge = _renforge_runtime.bridge
        operation = bridge.progression if bridge is not None else None
        if operation is None or not operation["active"]:
            return
        if _renforge_time.monotonic() >= operation["deadline"]:
            _renforge_progress_finish("timeout")
            return
        if operation["steps"] >= operation["max_steps"]:
            _renforge_progress_finish("max_steps")
            return
        # This line was already checked and is skipping; the engine ends it
        # after its skip delay. Another full check would only repeat it,
        # unless the screens are being rebuilt within the same interaction.
        if (operation["enabled_generation"] == bridge.observe_generation
                and (renpy.config.skipping == "slow"
                     or _renforge_time.monotonic() - (operation.get("clicked") or 0) < _RENFORGE_PROGRESS_RECLICK_S)
                and not getattr(renpy.display.interface, "restart_interaction", False)):
            return
        operation["clicked"] = None
        raw = _renforge_observe_raw(False, include_readout=False)
        if not raw["stable"]:
            if (raw.get("unstable_reason") in ("pause", "transition")
                    and getattr(renpy.config, "allow_skipping", True)
                    and getattr(renpy.store, "_skipping", True)):
                # The game moves on by itself, and skipping ends pauses and
                # transitions as Ren'Py's own skip does. Never judge the
                # screens behind it: they may be about to go (a
                # "show black; pause 0.5; jump" leaves the old hub up).
                renpy.config.skipping = "slow"
                operation["enabled_generation"] = bridge.observe_generation
                schedule = getattr(renpy.display.interface, "timeout", None)
                if callable(schedule):
                    schedule(0)
                return
            renpy.config.skipping = None
            return
        snapshot = _renforge_runtime.interaction.classify(raw)
        boundary = _renforge_runtime.interaction.progression_boundary(snapshot)
        if boundary is not None:
            _renforge_progress_finish(boundary)
            return
        if snapshot["omitted"]["unclassified"] or snapshot["omitted"]["focus_truncated"]:
            _renforge_progress_finish("unsupported")
            return
        if snapshot["forward"] == "dismiss":
            # Reaching a line already passed in this operation is a loop the
            # player must break (a prompt over a map, a retry after a bad
            # ending): hand control back instead of going round again.
            # The same statement showing the same words, run again; a scripted
            # loop that counts ("Line [n].") moves on by itself and is not one.
            # A line paused with {w} shows again from the same run of its say
            # statement (same say_id), so it is not one either.
            # A subroutine called from different places (a shared voice label each
            # scene line calls) runs its line again as the story moves on, so
            # where it was called from is part of the spot.
            where = raw.get("statement") or {}
            words = raw.get("dialogue") or {}
            spot = (where.get("file"), where.get("line"), raw.get("label"), words.get("who"), words.get("what"),
                    _renforge_call_sites())
            if where.get("file") is not None:
                said = operation["visited"].get(spot)
                if said is not None and said != bridge.say_id:
                    _renforge_progress_finish("loop")
                    return
                operation["visited"][spot] = bridge.say_id
            operation["enabled_generation"] = bridge.observe_generation
            # Save the rendered copy before the engine moves to another line.
            dialogue = raw.get("dialogue")
            if dialogue:
                for event in reversed(bridge.events):
                    if event.get("type") == "say" and event.get("say_id") == bridge.say_id:
                        event.update(dialogue)
                        break
            if not getattr(renpy.config, "allow_skipping", True) or not getattr(renpy.store, "_skipping", True):
                # The game turned skipping off (in a battle, say): click
                # through the line as a player would.
                # A click in the line's first frames can be dropped; the
                # line still being up later means it was, so click again.
                renpy.config.skipping = None
                operation["clicked"] = _renforge_time.monotonic()
                renpy.exports.queue_event("dismiss")
                return
            renpy.config.skipping = "slow"
            schedule = getattr(renpy.display.interface, "timeout", None)
            if callable(schedule):
                # Turning skip on programmatically must schedule the engine's
                # TIMEEVENT, as its keymap toggle normally restarts interaction.
                schedule(0)
        else:
            # Hard/timed pauses are allowed to elapse without forcing input.
            renpy.config.skipping = None

    def _renforge_h_progression(payload):
        """Start, poll or stop a bounded skip through dialogue the bridge owns.

        ``action: "start"`` takes an ``id``, the observation's
        ``interaction``/``session_id``/``observation_id``, ``max_steps``
        (1-200), ``timeout`` (0.05-120 s) and optional ``expires_at`` (epoch
        seconds); ``"status"`` and ``"stop"`` (optional ``reason``, default
        ``cancelled``) take the same ``id``. Replies ``{"ok": True,
        "progression": {"id", "active", "stop", "steps"}}``. Errors:
        ``stale_operation``, ``invalid_operation``, ``progression_busy``,
        ``invalid_budget``, ``expired``, ``already_skipping``,
        ``unsupported`` and the interaction contract's identity guards."""
        payload = payload or {}
        bridge = _renforge_runtime.bridge
        operation = bridge.progression
        operation_id = payload.get("id")
        if payload.get("action") == "status":
            # Cheap poll while an operation runs: no observation is built.
            if operation is None or operation["id"] != operation_id:
                return {"ok": False, "error": "stale_operation"}
            return {"ok": True, "progression": _renforge_progress_public(operation)}
        if payload.get("action") == "stop":
            if operation is None or operation["id"] != operation_id:
                return {"ok": False, "error": "stale_operation"}
            _renforge_progress_finish(payload.get("reason", "cancelled"))
            return {"ok": True, "progression": _renforge_progress_public(operation)}
        if payload.get("action") != "start" or not isinstance(operation_id, builtins.str) or not operation_id:
            return {"ok": False, "error": "invalid_operation"}
        if operation is not None and operation["active"]:
            return {"ok": False, "error": "progression_busy"}
        max_steps, timeout = payload.get("max_steps"), payload.get("timeout")
        if type(max_steps) is not builtins.int or not 1 <= max_steps <= 200:
            return {"ok": False, "error": "invalid_budget"}
        if not _renforge_runtime.interaction.is_real_number(timeout) or not 0.05 <= timeout <= 120:
            return {"ok": False, "error": "invalid_budget"}
        expires_at = payload.get("expires_at")
        if expires_at is not None:
            if not _renforge_runtime.interaction.is_real_number(expires_at):
                return {"ok": False, "error": "invalid_budget"}
            timeout = min(timeout, expires_at - _renforge_time.time())
            if timeout <= 0:
                return {"ok": False, "error": "expired"}
        raw = _renforge_observe_raw(False, include_readout=False)
        refusal = _renforge_runtime.interaction.guard_identity(raw, payload.get("interaction"),
            session_id=payload.get("session_id"), observation_id=payload.get("observation_id"))
        if refusal:
            return {"ok": False, "error": refusal}
        if getattr(renpy.config, "skipping", None):
            return {"ok": False, "error": "already_skipping"}
        prefs = getattr(renpy.store, "_preferences", None)
        if prefs is None:
            return {"ok": False, "error": "unsupported"}
        saved = {name: getattr(prefs, name) for name in ("skip_unseen", "skip_after_choices", "transitions", "text_cps") if hasattr(prefs, name)}
        if "skip_unseen" not in saved or "skip_after_choices" not in saved:
            return {"ok": False, "error": "unsupported"}
        bridge.progression = {
            "id": operation_id, "active": True, "stop": None, "steps": 0,
            "deadline": _renforge_time.monotonic() + timeout, "max_steps": max_steps,
            "preferences": prefs, "saved_preferences": saved, "enabled_generation": None,
            "skip_delay": getattr(renpy.config, "skip_delay", None),
            "visited": builtins.dict(), "clicked": None,
        }
        # Games may hold each skipped line on screen (one uses
        # 150 ms); each line is already checked after it draws.
        if bridge.progression["skip_delay"] is not None:
            renpy.config.skip_delay = 0
        prefs.skip_unseen = True
        prefs.skip_after_choices = False
        if "transitions" in saved:
            prefs.transitions = 0
        if "text_cps" in saved:
            prefs.text_cps = 0
        # Check each new line within milliseconds instead of at the engine's
        # 20 Hz periodic rate, which otherwise caps skipping at ~19 lines/s.
        _renforge_periodic_interval(_RENFORGE_PROGRESS_PERIODIC_MS)
        _renforge_progress_tick()
        return {"ok": True, "progression": _renforge_progress_public(bridge.progression)}


    def _renforge_observe_apply_facts(record, widget):
        facts = _renforge_observe_control_facts(widget)
        record["drag_name"] = facts.get("drag_name")
        record["tooltip"] = facts.get("tooltip")
        record["alternate"] = facts.get("alternate")
        record["hovered"] = facts.get("hovered")
        record["selected"] = facts.get("selected")
        record["adjustment"] = facts.get("adjustment")
        role = _renforge_observe_normalize_role(record.get("role"))
        operations = _renforge_observe_operations(role)
        if role == "input":
            # What is typed so far, as Ren'Py's Input holds it.
            content = getattr(widget, "content", None)
            record["value"] = content if isinstance(content, builtins.str) else None
        if role == "bar":
            adjustment = getattr(widget, "adjustment", None)
            # Ren'Py makes a bar focusable only when its adjustment is
            # adjustable; otherwise it only shows a value (a health bar).
            adjustable = bool(getattr(adjustment, "adjustable", True))
            operations = ["value"] if adjustable and callable(getattr(adjustment, "change", None)) else []
            # A viewport's scrollbar (Ren'Py builds it on the viewport's own
            # adjustment) has no value and no change callback: it only
            # scrolls. A slider bound to a value or callback is input.
            if adjustable and getattr(widget, "value", None) is None and getattr(adjustment, "changed", None) is None:
                record["scroll"] = True
        elif role == "viewport":
            operations = ["value"] if any(callable(getattr(getattr(widget, attr, None), "change", None)) for attr in ("xadjustment", "yadjustment")) else []
            record["scroll"] = True
        elif role == "drag":
            record["draggable"] = bool(getattr(widget, "draggable", False))
            record["droppable"] = bool(getattr(widget, "droppable", False))
            operations = []
            if record["draggable"]:
                operations.append("drop")
            if getattr(widget, "clicked", None) is not None:
                operations.append("click")
        record["operations"] = operations
        return record


    def _renforge_kinetic_kind(node):
        """``"bar"``, ``"viewport"`` or ``"drag"`` for those engine widgets, else None.

        Subclasses (VPGrid, a game's own Bar or Drag) count as their base
        class, as ``_renforge_is_engine_widget`` decides elsewhere in observe.
        """
        for name, kind in (("Bar", "bar"), ("Viewport", "viewport"), ("Drag", "drag")):
            if _renforge_is_engine_widget(node, name):
                return kind
        return None


    def _renforge_kinetic_enabled(widget, kind):
        if kind == "bar":
            adjustment = getattr(widget, "adjustment", None)
            adjustable = getattr(adjustment, "adjustable", True) if adjustment is not None else True
            return adjustable is not False
        if kind == "drag":
            if getattr(widget, "draggable", False) or getattr(widget, "droppable", False):
                return True
            if getattr(widget, "clicked", None) is not None or getattr(widget, "dragged", None) is not None:
                return True
            return False
        return True


    def _renforge_observe_kinetic_records(seen_widgets, inventory=None):
        if inventory is None:
            inventory = _renforge_scene_inventory()
        found = []
        for node, _layer, screen_name, _screen_node, _parents in inventory["nodes"]:
            kind = _renforge_kinetic_kind(node)
            if kind is not None and id(node) not in seen_widgets:
                found.append((node, screen_name, _screen_node, kind))
        records = []
        for ordinal, (widget, screen_name, screen_node, kind) in enumerate(found):
            enabled = _renforge_kinetic_enabled(widget, kind)
            image_name = _renforge_observe_image_name(widget) or _renforge_observe_drag_image(widget)
            record = {
                "screen": screen_name,
                "role": kind,
                # A viewport's content is readout, not its label.
                "text": None if _renforge_observe_normalize_role(kind) == "viewport" else (_renforge_focus_text(widget) or None),
                "enabled": enabled,
                "clickable": enabled,
                "covered": False,
                "visible": True,
                "image_name": image_name,
                "menu_index": None,
                "ordinal": ordinal,
                "action": _renforge_observe_action_class(widget),
            }
            records.append((types.SimpleNamespace(widget=widget, screen=screen_node), record))
        return records


    def _renforge_call_action(action, args):
        behavior = getattr(getattr(renpy, "display", None), "behavior", None)
        run = getattr(behavior, "run", None)
        if not callable(run):
            run = getattr(renpy, "run", None)
        if not callable(run):
            return False, None
        if args:
            return True, run(action, *args)
        return True, run(action)


    def _renforge_end_action_result(rv):
        if rv is None:
            return
        end_interaction = getattr(renpy, "end_interaction", None)
        if callable(end_interaction):
            end_interaction(rv)


    def _renforge_change_adjustment(adjustment, number):
        if adjustment is None:
            return False, None
        round_value = getattr(adjustment, "round_value", None)
        if callable(round_value):
            try:
                number = round_value(number, release=True)
            except TypeError:
                try:
                    number = round_value(number)
                except Exception:
                    pass
            except Exception:
                pass
        change = getattr(adjustment, "change", None)
        if not callable(change):
            return False, None
        rv = change(number)
        _renforge_end_action_result(rv)
        stored = getattr(adjustment, "value", number)
        if _renforge_observe_number(stored) is None:
            stored = number
        return True, stored


    def _renforge_act_refusal(error, interaction, control_id=None):
        """act's refusal: the error, the interaction, and the control id when known."""
        reply = {"ok": False, "error": error, "interaction": interaction}
        if control_id is not None:
            reply["id"] = control_id
        return reply


    def _renforge_act_value(payload, focus, result, control):
        """Set a bar's ``value``, or a viewport's ``x``/``y`` scroll, through its adjustment.

        ``result`` is act's success reply (operation, id, interaction, screen,
        via); the stored values are added to it and it is returned. A
        viewport changes no axis unless every requested axis can change.
        Refusals: ``value_required`` (no finite number given) and
        ``unsupported`` (the adjustment cannot change)."""
        widget = getattr(focus, "widget", None)
        refuse = lambda error: _renforge_act_refusal(error, result["interaction"], result["id"])
        real_number = _renforge_runtime.interaction.is_real_number
        if control.get("role") == "viewport":
            axes = [(axis, field) for axis, field in (("x", "xadjustment"), ("y", "yadjustment")) if axis in payload]
            if not axes:
                return refuse("value_required")
            for axis, field in axes:
                if not real_number(payload[axis]):
                    return refuse("value_required")
                if not callable(getattr(getattr(widget, field, None), "change", None)):
                    return refuse("unsupported")
            for axis, field in axes:
                changed, stored = _renforge_change_adjustment(getattr(widget, field, None), payload[axis])
                if not changed:
                    return refuse("unsupported")
                result[axis] = stored
            return result
        number = payload.get("value")
        if not real_number(number):
            return refuse("value_required")
        changed, stored = _renforge_change_adjustment(getattr(widget, "adjustment", None), number)
        if not changed:
            return refuse("unsupported")
        result["value"] = stored
        return result


    def _renforge_drag_joined(widget):
        joined = [widget]
        drag_joined = getattr(widget, "drag_joined", None)
        if not callable(drag_joined):
            return joined
        try:
            offsets = drag_joined(widget)
        except Exception:
            return joined
        if not _renforge_observe_is_sequence(offsets):
            return joined
        built = []
        for item in offsets:
            member = None
            if _renforge_observe_is_sequence(item) and item:
                member = item[0]
            elif item is not None:
                member = item
            resolver = getattr(widget, "get_drag_from_name", None)
            if member is not None and callable(resolver):
                member = resolver(member)
            if member is not None:
                built.append(member)
        if not built:
            return joined
        return built


    def _renforge_act_drop(payload, focus, result, pairs):
        """Drop a drag on the drag ``drop`` names (``""`` releases it on nothing).

        Runs the source's ``dragged`` callback, then the target's
        ``dropped`` when ``dragged`` returned nothing. With no ``drop`` key a
        drag that has a ``clicked`` action is clicked instead. ``result`` is
        act's success reply; it gains ``drop`` (or operation ``click``) and
        is returned. Refusals: ``missing`` (no such target), ``unsupported``
        (target not droppable or refusing, or no callback to run)."""
        widget = getattr(focus, "widget", None)
        wanted, interaction = result["id"], result["interaction"]
        if "drop" not in payload:
            clicked = getattr(widget, "clicked", None)
            if clicked is not None:
                if not _renforge_invoke_focus_action(focus):
                    return _renforge_act_refusal("unsupported", interaction, wanted)
                result["operation"] = "click"
                return result
            payload = dict(payload)
            payload["drop"] = ""
        drop_name = payload.get("drop")
        target = None
        if isinstance(drop_name, builtins.str) and drop_name:
            match = next(((other_focus, other) for other_focus, other in pairs if other.get("id") == drop_name), None)
            if match is None:
                return _renforge_act_refusal("missing", interaction, drop_name)
            other_focus, other = match
            target = getattr(other_focus, "widget", None)
            if other.get("role") != "drag" or not other.get("droppable") or not other.get("enabled", True) or other.get("covered"):
                return _renforge_act_refusal("unsupported", interaction, drop_name)
            drop_allowable = getattr(target, "drop_allowable", None)
            if callable(drop_allowable) and not drop_allowable(target, _renforge_drag_joined(widget)):
                return _renforge_act_refusal("unsupported", interaction, drop_name)
            result["drop"] = drop_name
        else:
            result["drop"] = None
        dragged = getattr(widget, "dragged", None)
        dropped = getattr(target, "dropped", None) if target is not None else None
        if dragged is None and dropped is None:
            return _renforge_act_refusal("unsupported", interaction, wanted)
        joined = _renforge_drag_joined(widget)
        rv = None
        if dragged is not None:
            called, rv = _renforge_call_action(dragged, (joined, target))
            if not called:
                return _renforge_act_refusal("unsupported", interaction, wanted)
        if rv is None and target is not None:
            dropped = getattr(target, "dropped", None)
            if dropped is not None:
                called, rv = _renforge_call_action(dropped, (target, joined))
                if not called:
                    return _renforge_act_refusal("unsupported", interaction, wanted)
        _renforge_end_action_result(rv)
        return result


    def _renforge_control_fingerprints(pairs):
        """What makes a control the same one: widget, action, and availability."""
        return builtins.dict(
            (record.get("id"), (id(getattr(focus, "widget", None)), id(_renforge_focus_action_value(focus)),
                                bool(record.get("enabled", True))))
            for focus, record in pairs if record.get("id"))

    def _renforge_accept_unchanged_control(payload, raw, pairs):
        """Let an action chosen from a recent observation through when only
        unrelated parts of the screen changed since (a notification closing,
        a HUD animating). Same session and interaction, and the target
        control is the same widget with the same action and availability.
        Anything else stays stale."""
        wanted = payload.get("observation_id")
        control = payload.get("id")
        if not wanted or not control or wanted == raw.get("observation_id"):
            return payload
        bridge = _renforge_runtime.bridge
        old = (getattr(bridge, "recent_observations", None) or {}).get(wanted)
        if old is None or old[0] != raw.get("session_id") or old[1] != raw.get("interaction"):
            return payload
        before = old[2].get(control)
        if before is None or before != _renforge_control_fingerprints(pairs).get(control):
            return payload
        payload = builtins.dict(payload)
        payload["observation_id"] = raw["observation_id"]
        return payload

    def _renforge_expand_act_input(payload, pairs):
        """Turn act's single ``input`` into what the control takes.

        Text for an input, a number for a bar, ``{"x", "y"}`` for a viewport,
        and a control id for a drag's drop target. The control's own
        operations decide; anything else is refused before any input.
        """
        if "input" not in payload:
            return payload, None
        payload = builtins.dict(payload)
        value = payload.pop("input")
        if value is None:
            return payload, None
        record = next((record for _focus, record in pairs if record.get("id") == payload.get("id")), None)
        if record is None:
            return payload, None  # the normal lookup reports the missing control
        operations = record.get("operations") or ()
        role = _renforge_observe_normalize_role(record.get("role"))
        number = _renforge_runtime.interaction.is_real_number(value)
        if "text" in operations and isinstance(value, builtins.str):
            payload["text"] = value
        elif "drop" in operations and isinstance(value, builtins.str):
            payload["drop"] = value
        elif role == "viewport" and isinstance(value, builtins.dict):
            for axis in ("x", "y"):
                if value.get(axis) is not None:
                    payload[axis] = value[axis]
        elif "value" in operations and number:
            payload["value"] = value
        else:
            return payload, "unsupported_input"
        return payload, None

    def _renforge_act_key(payload, interaction, bridge):
        """Press, hold (``hold: true``) or release (``hold: false``) the physical key ``key``.

        Replies with the key's folded name, ``hold`` and the posted
        ``events``; refuses an unknown key (listing the accepted ones) and a
        ``hold`` that is not a boolean (``unsupported``)."""
        key_value = payload.get("key")
        if not isinstance(key_value, builtins.str) or not key_value.strip():
            return {
                "ok": False,
                "error": "unknown key %r; accepted keys: %s" % (key_value, _RENFORGE_ACT_KEY_LIST),
                "interaction": interaction,
            }
        folded = key_value.strip().casefold()
        attr_name = _RENFORGE_ACT_KEY_ATTRS.get(folded)
        keycode = (
            getattr(pygame, attr_name, None)
            if pygame is not None and attr_name is not None
            else None
        )
        if attr_name is None or isinstance(keycode, bool) or not isinstance(keycode, builtins.int):
            if attr_name is not None and pygame is None:
                return {"ok": False, "error": "pygame_sdl2 event API is unavailable"}
            return {
                "ok": False,
                "error": "unknown key %r; accepted keys: %s" % (folded, _RENFORGE_ACT_KEY_LIST),
                "interaction": interaction,
            }
        if "hold" in payload:
            hold = payload.get("hold")
            if not isinstance(hold, bool):
                return _renforge_act_refusal("unsupported", interaction)
        else:
            hold = None
        event_names = _renforge_post_key(keycode, hold, folded if folded in _RENFORGE_ACT_KEY_LETTERS else "")
        if bridge is not None:
            if hold is True:
                bridge.held_keys.add(folded)
            else:
                bridge.held_keys.discard(folded)
        return {
            "ok": True,
            "operation": "key",
            "key": folded,
            "interaction": interaction,
            "hold": hold,
            "events": event_names,
        }

    def _renforge_h_act(payload):
        """Operate one control from a recent observe, or press a key.

        Payload: ``interaction``, ``session_id`` and ``observation_id`` from
        the observation, then either ``key`` (with optional ``hold``) or a
        control ``id`` with what it takes: ``text`` (and ``submit``),
        ``value``, ``x``/``y``, ``drop``, or the play tool's single ``input``.
        The control's operations decide: value, drop, click (its action, or
        a pointer click), or text. Replies ``{"ok": True, "operation", "id",
        "interaction", ...}``; refusals carry ``error`` (``stale``,
        ``missing``, ``unsupported``, ``value_required``, ``text_required``,
        ``unsupported_input`` and the interaction contract's guards). When
        the action steers the script (a Jump), the success reply rides on
        the exception as ``renforge_result``."""
        payload = payload or {}
        contract = _renforge_runtime.interaction
        raw, pairs = _renforge_observe_raw(False, with_pairs=True, include_readout=False)
        payload = _renforge_accept_unchanged_control(payload, raw, pairs)
        payload, refusal = _renforge_expand_act_input(payload, pairs)
        if refusal is None:
            refusal = contract.validate_act_payload(payload)
        if refusal:
            return {"ok": False, "error": refusal}
        interaction = raw["interaction"]
        bridge = _renforge_runtime.bridge
        key = payload.get("key")
        releasing = (payload.get("hold") is False and isinstance(key, builtins.str)
                     and key.strip().casefold() in getattr(bridge, "held_keys", set()))
        # A key we hold must be releasable after it changes the interaction.
        # Session ownership still applies; unrelated key-up requests are guarded.
        if releasing:
            if payload.get("session_id") is not None and payload["session_id"] != raw["session_id"]:
                return _renforge_act_refusal("stale", interaction)
        else:
            refusal = contract.guard_identity(
                raw, payload.get("interaction"), session_id=payload.get("session_id"),
                observation_id=payload.get("observation_id"))
            if refusal:
                return {"ok": False, "error": refusal, "interaction": interaction, "observation": raw}
        if payload.get("key") is not None:
            return _renforge_act_key(payload, interaction, bridge)
        wanted = payload.get("id")
        if not isinstance(wanted, builtins.str) or not wanted:
            return _renforge_act_refusal("missing", interaction)
        match = next(((focus, control) for focus, control in pairs if control.get("id") == wanted), None)
        if match is None:
            return _renforge_act_refusal("missing", interaction, wanted)
        focus, control = match
        snapshot = contract.classify(raw)
        refusal = contract.guard_act(
            snapshot, payload.get("interaction"), wanted,
            text=payload.get("text"), value=payload.get("value"),
            x=payload.get("x"), y=payload.get("y"), drop=payload.get("drop"),
            session_id=payload.get("session_id"), observation_id=payload.get("observation_id"))
        if refusal:
            return {"ok": False, "error": refusal, "interaction": interaction, "id": wanted, "observation": raw}
        operations = control.get("operations") or []
        if "value" in operations or "drop" in operations:
            result = {
                "ok": True,
                "operation": "value" if "value" in operations else "drop",
                "id": wanted,
                "interaction": interaction,
                "screen": control.get("screen"),
                "via": "action",
            }
            if result["operation"] == "value":
                run = lambda: _renforge_act_value(payload, focus, result, control)
            else:
                result["action"] = control.get("action")
                run = lambda: _renforge_act_drop(payload, focus, result, pairs)
            try:
                return _renforge_with_focus_screen(focus, run)
            except Exception as exc:
                if _renforge_is_script_control_flow(exc):
                    setattr(exc, "renforge_result", result)
                raise
        if "click" in operations:
            result = {
                "ok": True,
                "operation": "click",
                "id": wanted,
                "interaction": interaction,
                "screen": control.get("screen"),
                "action": control.get("action"),
            }
            try:
                if _renforge_invoke_focus_action(focus):
                    result["via"] = "action"
                    return result
            except Exception as exc:
                if not _renforge_is_script_control_flow(exc):
                    raise
                result["via"] = "action"
                setattr(exc, "renforge_result", result)
                raise
            result["via"] = "pointer"
            try:
                x, y = _renforge_click_focus(focus, control)
            except Exception as exc:
                if not _renforge_is_script_control_flow(exc):
                    raise
                pointer = getattr(exc, "renforge_pointer", None)
                if pointer:
                    result["x"], result["y"] = pointer
                setattr(exc, "renforge_result", result)
                raise
            result["x"] = x
            result["y"] = y
            return result
        if "text" in operations:
            text = payload.get("text")
            if not isinstance(text, str):
                return _renforge_act_refusal("text_required", interaction, wanted)
            if pygame is None:
                return {"ok": False, "error": "pygame_sdl2 event API is unavailable"}
            widget = getattr(focus, "widget", None)
            force_focus = getattr(renpy.display.focus, "force_focus", None)
            if widget is not None and callable(force_focus):
                try:
                    force_focus(widget)
                except Exception:
                    pass
            else:
                change_focus = getattr(renpy.display.focus, "change_focus", None)
                if callable(change_focus):
                    try:
                        change_focus(focus)
                    except Exception:
                        pass
            result = _renforge_type_input(widget, text, payload.get("submit", True))
            result.update({"operation": "text", "id": wanted, "interaction": interaction})
            return result
        return _renforge_act_refusal("unsupported", interaction, wanted)

    _RENFORGE_HANDLERS = {
        "ping": _renforge_h_ping,
        "get_state": _renforge_h_get_state,
        "session_info": _renforge_h_session_info,
        "get_metrics": _renforge_h_get_metrics_handler,
        "get_audio_state": _renforge_h_get_audio_state_handler,
        "inspect_screen": _renforge_h_inspect_screen,
        "eval": _renforge_h_eval,
        "get_var": _renforge_h_get_var,
        "set_var": _renforge_h_set_var,
        "screenshot": _renforge_h_screenshot,
        "observe": _renforge_h_observe,
        "explain": _renforge_h_explain,
        "act": _renforge_h_act,
        "dismiss_if": _renforge_h_dismiss_if,
        "progression": _renforge_h_progression,
        "advance": _renforge_h_advance,
        "send_input": _renforge_h_send_input,
        "control": _renforge_h_control,
        "save_slot": _renforge_h_save_slot,
        "load_slot": _renforge_h_load_slot,
        "list_slots": _renforge_h_list_slots,
        "poll_events": _renforge_h_poll_events,
        "list_choices": _renforge_h_list_choices,
        "list_menu_choices": _renforge_h_list_menu_choices,
        "select_choice": _renforge_h_select_choice,
        "list_ui_elements": _renforge_h_list_ui_elements,
        "click_element": _renforge_h_click_element,
        "hover_element": _renforge_h_hover_element,
        "get_ui_element_bounds": _renforge_h_get_ui_element_bounds,
        "click_at": _renforge_h_click_at,
        "hit_test": _renforge_h_hit_test,
        "get_displayable_bounds": _renforge_h_get_displayable_bounds,
        "show_displayable": _renforge_h_show_displayable,
        "scene_tree": _renforge_h_scene_tree,
    }

    def _renforge_skip_stop_reason():
        """Infer why Skip stopped from the current interactive context."""
        bridge = _renforge_runtime.bridge
        if bridge is not None and getattr(bridge, "_skip_reason_hint", None):
            hint = bridge._skip_reason_hint
            bridge._skip_reason_hint = None
            return hint
        try:
            if renpy.get_screen("choice") is not None:
                return "choice"
        except Exception:
            pass
        try:
            # Unseen dialogue policy left skip off after a line the player has not seen.
            prefs = getattr(renpy.store, "_preferences", None)
            if prefs is not None and not bool(getattr(prefs, "skip_unseen", True)):
                return "unseen_dialogue"
        except Exception:
            pass
        try:
            if not renpy.is_in_test() and getattr(renpy.context(), "current", None) is None:
                return "end_of_context"
        except Exception:
            pass
        return "explicit_stop"

    def _renforge_watch_runtime_effects():
        """Emit skip/auto business events when engine state changes."""
        bridge = _renforge_runtime.bridge
        if bridge is None:
            return
        try:
            skipping = getattr(renpy.config, "skipping", None)
        except Exception:
            skipping = None
        prev_skip = bridge.prev_skipping
        progression = getattr(bridge, "progression", None)
        if progression is not None and progression.get("active"):
            # A progression turns skipping on and off itself, line by line;
            # that is not the player skipping.
            pass
        elif prev_skip and not skipping:
            screen = None
            try:
                if renpy.get_screen("choice") is not None:
                    screen = "choice"
            except Exception:
                pass
            _renforge_emit_business(
                "skip.stopped",
                reason=_renforge_skip_stop_reason(),
                screen=screen,
                previous=prev_skip,
            )
        elif skipping and not prev_skip:
            _renforge_emit_business("skip.started", mode=skipping)
        bridge.prev_skipping = skipping

        try:
            prefs = getattr(renpy.store, "_preferences", None)
            afm = bool(getattr(prefs, "afm_enable", False)) if prefs is not None else None
        except Exception:
            afm = None
        if afm is not None and bridge.prev_afm is not None and afm != bridge.prev_afm:
            _renforge_emit_business("auto.changed", enabled=afm)
        if afm is not None:
            bridge.prev_afm = afm

        history = _renforge_history_index()
        if history is not None:
            bridge.prev_history_index = history

    def _renforge_start_at_label():
        """Leave for RENFORGE_BRIDGE_START_LABEL once, as the main menu's Start does.

        Waits for a real interaction past Ren'Py's own startup labels. From the
        main menu it leaves that context like ``Start(label)``; from a splash
        screen it jumps. Nothing the story did before the label has happened.
        """
        import os as _os  # a reload_script store wipe removes module globals

        bridge = _renforge_runtime.bridge
        label = _os.environ.get("RENFORGE_BRIDGE_START_LABEL", "").strip()
        if bridge is None or not label or getattr(bridge, "start_label_state", None) is not None:
            return
        try:
            if not renpy.game.context().interacting:
                return
        except Exception:
            return
        phase = _renforge_game_lifecycle()["phase"]
        current = bridge.current_label if isinstance(bridge.current_label, builtins.str) else ""
        if phase != "main_menu" and (not current or current.startswith("_")):
            return
        if not renpy.has_label(label):
            bridge.start_label_state = {"label": label, "error": "unknown label"}
            return
        bridge.start_label_state = {"label": label}
        if phase == "main_menu":
            renpy.jump_out_of_context(label)
        renpy.jump(label)

    def renforge_drain_bridge():
        # Runs on the MAIN thread via config.periodic_callbacks.
        bridge = _renforge_runtime.bridge
        if bridge is None:
            return
        if bridge.stop.is_set():
            _renforge_pointer_tick(cancel=True)
            _renforge_progress_finish("cancelled")
            _renforge_reset_testmouse_state()
            return
        if _renforge_pointer_tick():
            return
        _renforge_start_at_label()
        _renforge_progress_tick()
        _renforge_watch_runtime_effects()
        while True:
            try:
                req = bridge.requests.get_nowait()
            except queue.Empty:
                break
            handler = _RENFORGE_HANDLERS.get(req.command)
            correlation = None
            explicit_correlation = None
            propagate = None
            try:
                if handler is None:
                    req.error = "unknown_command: %s" % req.command
                else:
                    explicit_correlation = _renforge_correlation_from_payload(req.payload)
                    correlation = explicit_correlation
                    if correlation is None and req.command in (
                        "control",
                        "click_element",
                        "save_slot",
                        "load_slot",
                    ):
                        # Auto ids keep business events attributable even when
                        # the caller omitted interaction_id; only explicit ids
                        # are echoed on the command reply.
                        bridge.interaction_counter += 1
                        correlation = "%s-%s" % (req.command, bridge.interaction_counter)
                    bridge.current_correlation_id = correlation
                    bridge.current_request = req
                    outer_nested = getattr(bridge, "nested_reply_request", None)
                    bridge.nested_reply_request = req
                    try:
                        result = handler(req.payload)
                    finally:
                        bridge.nested_reply_request = outer_nested
                    if (
                        isinstance(result, builtins.dict)
                        and explicit_correlation is not None
                    ):
                        result = builtins.dict(result)
                        result.setdefault("interaction_id", explicit_correlation)
                    req.result = result
            except Exception as exc:
                if _renforge_is_script_control_flow(exc):
                    preserved_result = getattr(exc, "renforge_result", None)
                    if isinstance(preserved_result, builtins.dict):
                        preserved_result = builtins.dict(preserved_result)
                        preserved_result["ended_interaction"] = True
                        if explicit_correlation is not None:
                            preserved_result.setdefault("interaction_id", explicit_correlation)
                        req.result = preserved_result
                    else:
                        req.result = {"ok": True, "ended_interaction": True}
                    propagate = exc
                else:
                    req.error = "%s: %s" % (type(exc).__name__, exc)
            finally:
                bridge.current_correlation_id = None
                bridge.current_request = None
                pending = getattr(bridge, "pointer_click", None)
                if pending is None:
                    _renforge_reset_testmouse_state()
                    req.event.set()
                elif req.error is not None or propagate is not None:
                    _renforge_pointer_tick(cancel=True)
            if propagate is not None:
                raise propagate
            if getattr(bridge, "pointer_click", None) is not None:
                break

    # --- listener: background thread --------------------------------------

    def _renforge_reply(conn, obj):
        # Local import: the listener thread survives renpy.reload_script(),
        # which wipes the store (the __globals__ of init-python functions).
        # A free-var reference to ``json`` would raise NameError mid-reload.
        # ``import`` reads from sys.modules, which the reload never touches.
        import json as _json
        conn.sendall((_json.dumps(obj) + "\n").encode("utf-8"))

    _RENFORGE_BRIDGE_INFO_MAX_BYTES = 16 * 1024
    _RENFORGE_BRIDGE_STARTUP_ERROR_PREFIX = "RENFORGE_BRIDGE_STARTUP_ERROR="
    _RENFORGE_BRIDGE_STARTUP_PUBLICATION_FAILED = "BRIDGE_MANIFEST_PUBLICATION_FAILED"
    _RENFORGE_BRIDGE_STARTUP_INFO_CONFLICT = "BRIDGE_INFO_CONFLICT"
    _RENFORGE_BRIDGE_STARTUP_IDENTITY_MISMATCH = "BRIDGE_MANIFEST_IDENTITY_MISMATCH"
    _RENFORGE_BRIDGE_INFO_KEYS = (
        "schema_version",
        "protocol_version",
        "state",
        "session_id",
        "project_root",
        "host",
        "port",
        "token",
    )

    def _renforge_bridge_startup_error(code):
        import sys as _sys
        try:
            _sys.stderr.write("%s%s\n" % (_RENFORGE_BRIDGE_STARTUP_ERROR_PREFIX, code))
            _sys.stderr.flush()
        except Exception:
            pass

    def _renforge_bridge_is_session_id(value):
        if not isinstance(value, str) or len(value) != 32:
            return False
        for ch in value:
            if ch not in "0123456789abcdef":
                return False
        return True

    def _renforge_bridge_is_token(value):
        if not isinstance(value, str) or len(value) != 64:
            return False
        for ch in value:
            if ch not in "0123456789abcdef":
                return False
        return True

    def _renforge_bridge_control_dir(project_root):
        """The private control directory: RENFORGE_BRIDGE_CONTROL_DIR, else
        ``<project_root>/.renforge/control``.

        RenForge keeps its state outside the game and the launcher says
        where. Like the project root, a configured directory must be an
        absolute path with no symlink on it (trailing separators aside):
        with a trailing separator, lstat follows a linked directory and the
        parent check looks at the directory itself, so the no-follow checks
        could be stepped around. Raises OSError otherwise."""
        import os as _os
        configured = (_os.environ.get("RENFORGE_BRIDGE_CONTROL_DIR") or "").strip()
        if configured:
            path = configured.rstrip(_os.sep + (_os.altsep or ""))
            if not path or not _os.path.isabs(path) or _os.path.realpath(path) != path:
                raise OSError("control directory must be an absolute path without symlinks")
            return path
        return _os.path.join(project_root, ".renforge", "control")

    def _renforge_bridge_info_path(project_root):
        import os as _os
        return _os.path.join(_renforge_bridge_control_dir(project_root), "bridge.json")

    def _renforge_bridge_win_create_file(path, access, share_mode, creation_disposition, flags_and_attrs):
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        # WinDLL + pointer-width template HANDLE (NULL) avoids 32-bit coercion.
        kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            _wintypes.LPCWSTR,
            _wintypes.DWORD,
            _wintypes.DWORD,
            _ctypes.c_void_p,
            _wintypes.DWORD,
            _wintypes.DWORD,
            _ctypes.c_void_p,
        ]
        create_file.restype = _wintypes.HANDLE
        handle = create_file(
            str(path),
            access,
            share_mode,
            None,
            creation_disposition,
            flags_and_attrs,
            None,
        )
        invalid_val = getattr(_wintypes, "HANDLE", _ctypes.c_void_p)(-1).value
        if handle == invalid_val or handle is None or handle == 0 or handle == 0xFFFFFFFF or handle == 0xFFFFFFFFFFFFFFFF:
            err = _ctypes.get_last_error()
            raise OSError(err, "CreateFileW failed for %s" % path)
        return handle

    def _renforge_bridge_win_close_handle(handle):
        import ctypes as _ctypes
        if handle is None:
            return
        try:
            _ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass

    def _renforge_bridge_win_get_file_type(handle):
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        get_type = _ctypes.windll.kernel32.GetFileType
        get_type.argtypes = [_wintypes.HANDLE]
        get_type.restype = _wintypes.DWORD
        return int(get_type(handle))

    def _renforge_bridge_win_get_handle_attributes(handle):
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        class _BY_HANDLE_FILE_INFORMATION(_ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", _wintypes.DWORD),
                ("ftCreationTime", _wintypes.FILETIME),
                ("ftLastAccessTime", _wintypes.FILETIME),
                ("ftLastWriteTime", _wintypes.FILETIME),
                ("dwVolumeSerialNumber", _wintypes.DWORD),
                ("nFileSizeHigh", _wintypes.DWORD),
                ("nFileSizeLow", _wintypes.DWORD),
                ("nNumberOfLinks", _wintypes.DWORD),
                ("nFileIndexHigh", _wintypes.DWORD),
                ("nFileIndexLow", _wintypes.DWORD),
            ]

        info = _BY_HANDLE_FILE_INFORMATION()
        get_info = _ctypes.windll.kernel32.GetFileInformationByHandle
        get_info.argtypes = [_wintypes.HANDLE, _ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
        get_info.restype = _wintypes.BOOL
        if not get_info(handle, _ctypes.byref(info)):
            raise OSError(_ctypes.GetLastError(), "GetFileInformationByHandle failed")
        return int(info.dwFileAttributes)

    def _renforge_bridge_win_read_handle(handle, max_bytes):
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        read_file = _ctypes.windll.kernel32.ReadFile
        read_file.argtypes = [
            _wintypes.HANDLE,
            _ctypes.c_void_p,
            _wintypes.DWORD,
            _ctypes.POINTER(_wintypes.DWORD),
            _ctypes.c_void_p,
        ]
        read_file.restype = _wintypes.BOOL

        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            size = min(65536, remaining)
            buf = _ctypes.create_string_buffer(size)
            read_bytes = _wintypes.DWORD(0)
            if not read_file(handle, buf, size, _ctypes.byref(read_bytes), None):
                raise OSError(_ctypes.GetLastError(), "ReadFile failed")
            if read_bytes.value == 0:
                break
            chunks.append(buf.raw[: read_bytes.value])
            remaining -= read_bytes.value
        return b"".join(chunks)

    def _renforge_bridge_win_write_handle(handle, data):
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        write_file = _ctypes.windll.kernel32.WriteFile
        write_file.argtypes = [
            _wintypes.HANDLE,
            _ctypes.c_void_p,
            _wintypes.DWORD,
            _ctypes.POINTER(_wintypes.DWORD),
            _ctypes.c_void_p,
        ]
        write_file.restype = _wintypes.BOOL

        written = 0
        while written < len(data):
            to_write = min(65536, len(data) - written)
            buf = _ctypes.create_string_buffer(data[written : written + to_write])
            chunk_written = _wintypes.DWORD(0)
            if not write_file(handle, buf, to_write, _ctypes.byref(chunk_written), None):
                raise OSError(_ctypes.GetLastError(), "WriteFile failed")
            if chunk_written.value == 0:
                raise OSError("short write on Win32 handle")
            written += chunk_written.value

    def _renforge_bridge_win_truncate_handle(handle):
        """Seek to start and truncate so a shorter ready payload does not leave tails."""
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
        set_pointer = kernel32.SetFilePointer
        set_pointer.argtypes = [
            _wintypes.HANDLE,
            _ctypes.c_long,
            _ctypes.c_void_p,
            _wintypes.DWORD,
        ]
        set_pointer.restype = _wintypes.DWORD
        set_eof = kernel32.SetEndOfFile
        set_eof.argtypes = [_wintypes.HANDLE]
        set_eof.restype = _wintypes.BOOL
        if set_pointer(handle, 0, None, 0) == 0xFFFFFFFF:
            raise OSError(_ctypes.get_last_error(), "SetFilePointer failed")
        if not set_eof(handle):
            raise OSError(_ctypes.get_last_error(), "SetEndOfFile failed")

    def _renforge_bridge_win_flush_handle(handle):
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        flush = _ctypes.windll.kernel32.FlushFileBuffers
        flush.argtypes = [_wintypes.HANDLE]
        flush.restype = _wintypes.BOOL
        if not flush(handle):
            raise OSError(_ctypes.GetLastError(), "FlushFileBuffers failed")

    def _renforge_bridge_win_replace_file(replaced_path, replacement_path, flags=1):
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes
        import os as _os

        # Replacing a private (protected-DACL) destination via MoveFileEx
        # REPLACE_EXISTING returns ERROR_ACCESS_DENIED (5) on Windows CI even
        # when the caller owns FA. Clear attributes, remove the destination,
        # then rename the temp into place — same effective outcome as replace.
        kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
        set_attrs = kernel32.SetFileAttributesW
        set_attrs.argtypes = [_wintypes.LPCWSTR, _wintypes.DWORD]
        set_attrs.restype = _wintypes.BOOL
        FILE_ATTRIBUTE_NORMAL = 0x80

        for candidate in (replaced_path, replacement_path):
            try:
                set_attrs(str(candidate), FILE_ATTRIBUTE_NORMAL)
            except Exception:
                pass

        if _os.path.lexists(replaced_path) or _renforge_bridge_path_is_symlink(replaced_path):
            try:
                _os.unlink(replaced_path)
            except OSError as exc:
                raise OSError(getattr(exc, "winerror", None) or exc.errno or 5,
                              "failed to remove previous bridge info: %s" % replaced_path) from exc

        move_file = kernel32.MoveFileExW
        move_file.argtypes = [_wintypes.LPCWSTR, _wintypes.LPCWSTR, _wintypes.DWORD]
        move_file.restype = _wintypes.BOOL
        # MOVEFILE_WRITE_THROUGH only — destination is already gone.
        if move_file(str(replacement_path), str(replaced_path), 0x8):
            return True
        err = _ctypes.get_last_error()
        # Fallback: Python's os.replace (MoveFileEx REPLACE_EXISTING).
        try:
            _os.replace(str(replacement_path), str(replaced_path))
            return True
        except OSError:
            pass
        raise OSError(err, "MoveFileExW failed for %s -> %s" % (replacement_path, replaced_path))

    def _renforge_bridge_win_is_reparse(path):
        import os as _os
        try:
            st = _os.lstat(path)
            attrs = getattr(st, "st_file_attributes", None)
            if attrs is not None:
                return bool(attrs & 0x400)
        except OSError:
            pass
        try:
            import ctypes as _ctypes
            from ctypes import wintypes as _wintypes
            get_attrs = _ctypes.windll.kernel32.GetFileAttributesW
            get_attrs.argtypes = [_wintypes.LPCWSTR]
            get_attrs.restype = _wintypes.DWORD
            val = int(get_attrs(str(path)))
            if val != 0xFFFFFFFF:
                return bool(val & 0x400)
        except Exception:
            pass
        return False

    def _renforge_bridge_win_advapi_kernel():
        """Load kernel32/advapi32 with WinDLL so GetLastError is reliable."""
        import ctypes as _ctypes

        kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = _ctypes.WinDLL("advapi32", use_last_error=True)
        return kernel32, advapi32

    def _renforge_bridge_win_bind_sid_apis(kernel32, advapi32):
        """Declare pointer-width argtypes for SID helpers.

        Without argtypes, ctypes coerces PSID through c_int (32-bit). On
        Windows 64-bit that raises OverflowError and the bridge fails closed
        as BRIDGE_INFO_CONFLICT while validating the private control DACL.
        """
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        if getattr(advapi32, "_renforge_sid_bound", False):
            return

        advapi32.OpenProcessToken.argtypes = [
            _wintypes.HANDLE,
            _wintypes.DWORD,
            _ctypes.POINTER(_wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = _wintypes.BOOL

        advapi32.GetTokenInformation.argtypes = [
            _wintypes.HANDLE,
            _wintypes.DWORD,
            _ctypes.c_void_p,
            _wintypes.DWORD,
            _ctypes.POINTER(_wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = _wintypes.BOOL

        advapi32.ConvertSidToStringSidW.argtypes = [
            _ctypes.c_void_p,
            _ctypes.POINTER(_wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = _wintypes.BOOL

        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            _wintypes.LPCWSTR,
            _wintypes.DWORD,
            _ctypes.POINTER(_ctypes.c_void_p),
            _ctypes.POINTER(_wintypes.ULONG),
        ]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = _wintypes.BOOL

        # SetFileSecurityW ignores PROTECTED_DACL; SetNamedSecurityInfoW sets it.
        advapi32.SetNamedSecurityInfoW.argtypes = [
            _wintypes.LPWSTR,
            _wintypes.DWORD,
            _wintypes.DWORD,
            _ctypes.c_void_p,
            _ctypes.c_void_p,
            _ctypes.c_void_p,
            _ctypes.c_void_p,
        ]
        advapi32.SetNamedSecurityInfoW.restype = _wintypes.DWORD

        advapi32.GetSecurityDescriptorDacl.argtypes = [
            _ctypes.c_void_p,
            _ctypes.POINTER(_wintypes.BOOL),
            _ctypes.POINTER(_ctypes.c_void_p),
            _ctypes.POINTER(_wintypes.BOOL),
        ]
        advapi32.GetSecurityDescriptorDacl.restype = _wintypes.BOOL

        advapi32.GetNamedSecurityInfoW.argtypes = [
            _wintypes.LPCWSTR,
            _wintypes.DWORD,
            _wintypes.DWORD,
            _ctypes.POINTER(_ctypes.c_void_p),
            _ctypes.POINTER(_ctypes.c_void_p),
            _ctypes.POINTER(_ctypes.c_void_p),
            _ctypes.POINTER(_ctypes.c_void_p),
            _ctypes.POINTER(_ctypes.c_void_p),
        ]
        advapi32.GetNamedSecurityInfoW.restype = _wintypes.DWORD

        advapi32.GetSecurityDescriptorControl.argtypes = [
            _ctypes.c_void_p,
            _ctypes.POINTER(_wintypes.DWORD),
            _ctypes.POINTER(_wintypes.DWORD),
        ]
        advapi32.GetSecurityDescriptorControl.restype = _wintypes.BOOL

        advapi32.GetAce.argtypes = [
            _ctypes.c_void_p,
            _wintypes.DWORD,
            _ctypes.POINTER(_ctypes.c_void_p),
        ]
        advapi32.GetAce.restype = _wintypes.BOOL

        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = _wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [_wintypes.HANDLE]
        kernel32.CloseHandle.restype = _wintypes.BOOL
        kernel32.LocalFree.argtypes = [_ctypes.c_void_p]
        kernel32.LocalFree.restype = _ctypes.c_void_p

        advapi32._renforge_sid_bound = True

    def _renforge_bridge_win_sid_to_string(advapi32, kernel32, sid_ptr):
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        if not sid_ptr:
            raise OSError("null SID pointer")
        if not isinstance(sid_ptr, _ctypes.c_void_p):
            sid_ptr = _ctypes.c_void_p(int(sid_ptr))
        string_sid = _wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, _ctypes.byref(string_sid)):
            raise OSError(_ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            value = string_sid.value
            if not value:
                raise OSError("ConvertSidToStringSidW returned an empty SID")
            return str(value)
        finally:
            if string_sid:
                kernel32.LocalFree(string_sid)

    def _renforge_bridge_win_current_sid():
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        kernel32, advapi32 = _renforge_bridge_win_advapi_kernel()
        _renforge_bridge_win_bind_sid_apis(kernel32, advapi32)

        token = _wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            0x0008,  # TOKEN_QUERY
            _ctypes.byref(token),
        ):
            raise OSError(_ctypes.get_last_error(), "OpenProcessToken failed")
        try:
            size = _wintypes.DWORD(0)
            # First call sizes the buffer; ERROR_INSUFFICIENT_BUFFER is expected.
            advapi32.GetTokenInformation(token, 1, None, 0, _ctypes.byref(size))
            if size.value == 0:
                raise OSError(_ctypes.get_last_error(), "GetTokenInformation size query failed")
            buf = _ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, 1, buf, size.value, _ctypes.byref(size)):
                raise OSError(_ctypes.get_last_error(), "GetTokenInformation failed")

            class _SID_AND_ATTRIBUTES(_ctypes.Structure):
                _fields_ = [("Sid", _ctypes.c_void_p), ("Attributes", _wintypes.DWORD)]

            class _TOKEN_USER(_ctypes.Structure):
                _fields_ = [("User", _SID_AND_ATTRIBUTES)]

            user = _ctypes.cast(buf, _ctypes.POINTER(_TOKEN_USER)).contents
            return _renforge_bridge_win_sid_to_string(advapi32, kernel32, user.User.Sid)
        finally:
            kernel32.CloseHandle(token)

    def _renforge_bridge_win_set_protected_dacl(path):
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        kernel32, advapi32 = _renforge_bridge_win_advapi_kernel()
        _renforge_bridge_win_bind_sid_apis(kernel32, advapi32)

        sddl = "D:P(A;;FA;;;%s)(A;;FA;;;SY)(A;;FA;;;BA)" % _renforge_bridge_win_current_sid()
        sd = _ctypes.c_void_p()
        size = _wintypes.ULONG()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, _ctypes.byref(sd), _ctypes.byref(size)
        ):
            raise OSError(
                _ctypes.get_last_error(),
                "ConvertStringSecurityDescriptorToSecurityDescriptorW failed",
            )
        try:
            dacl_present = _wintypes.BOOL(0)
            dacl_defaulted = _wintypes.BOOL(0)
            dacl = _ctypes.c_void_p()
            if not advapi32.GetSecurityDescriptorDacl(
                sd,
                _ctypes.byref(dacl_present),
                _ctypes.byref(dacl),
                _ctypes.byref(dacl_defaulted),
            ):
                raise OSError(_ctypes.get_last_error(), "GetSecurityDescriptorDacl failed")
            if not dacl_present or not dacl:
                raise OSError("security descriptor has no DACL")
            # SE_FILE_OBJECT=1; DACL_SECURITY_INFORMATION|PROTECTED_DACL_SECURITY_INFORMATION
            status = advapi32.SetNamedSecurityInfoW(
                str(path),
                1,
                0x00000004 | 0x80000000,
                None,
                None,
                dacl,
                None,
            )
            if status != 0:
                raise OSError(status, "SetNamedSecurityInfoW failed for %s" % path)
        finally:
            if sd:
                kernel32.LocalFree(sd)

    def _renforge_bridge_win_validate_protected_dacl(path):
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        kernel32, advapi32 = _renforge_bridge_win_advapi_kernel()
        _renforge_bridge_win_bind_sid_apis(kernel32, advapi32)

        class _ACL(_ctypes.Structure):
            _fields_ = [
                ("AclRevision", _wintypes.BYTE),
                ("Sbz1", _wintypes.BYTE),
                ("AclSize", _wintypes.WORD),
                ("AceCount", _wintypes.WORD),
                ("Sbz2", _wintypes.WORD),
            ]

        class _ACE_HEADER(_ctypes.Structure):
            _fields_ = [("AceType", _wintypes.BYTE), ("AceFlags", _wintypes.BYTE), ("AceSize", _wintypes.WORD)]

        sd = _ctypes.c_void_p()
        dacl = _ctypes.c_void_p()
        status = advapi32.GetNamedSecurityInfoW(
            str(path),
            1,  # SE_FILE_OBJECT
            0x00000004,  # DACL_SECURITY_INFORMATION
            None,
            None,
            _ctypes.byref(dacl),
            None,
            _ctypes.byref(sd),
        )
        if status != 0:
            raise OSError(status, "GetNamedSecurityInfoW failed for %s" % path)
        try:
            if not dacl:
                raise OSError("missing DACL on %s" % path)
            control = _wintypes.DWORD()
            revision = _wintypes.DWORD()
            if not advapi32.GetSecurityDescriptorControl(
                sd, _ctypes.byref(control), _ctypes.byref(revision)
            ):
                raise OSError(_ctypes.get_last_error(), "GetSecurityDescriptorControl failed")
            if not (control.value & 0x1000):  # SE_DACL_PROTECTED
                raise OSError("DACL is not protected on %s" % path)

            allowed = {_renforge_bridge_win_current_sid(), "S-1-5-18", "S-1-5-32-544"}
            protected = bool(control.value & 0x1000)  # SE_DACL_PROTECTED
            inherited_ace = False
            acl = _ctypes.cast(dacl, _ctypes.POINTER(_ACL)).contents
            for index in range(acl.AceCount):
                ace = _ctypes.c_void_p()
                if not advapi32.GetAce(dacl, index, _ctypes.byref(ace)):
                    raise OSError(_ctypes.get_last_error(), "GetAce failed")
                header = _ctypes.cast(ace, _ctypes.POINTER(_ACE_HEADER)).contents
                if header.AceType != 0:  # ACCESS_ALLOWED_ACE_TYPE
                    raise OSError("unexpected ACE type on %s" % path)
                if header.AceFlags & 0x10:  # INHERITED_ACE
                    inherited_ace = True
                # ACCESS_ALLOWED_ACE: header (4) + Mask (4) + SidStart
                sid_ptr = _ctypes.c_void_p(int(ace.value or 0) + 8)
                sid_text = _renforge_bridge_win_sid_to_string(advapi32, kernel32, sid_ptr)
                if sid_text not in allowed:
                    raise OSError("unexpected trustee on private path %s" % path)
            # Prefer SE_DACL_PROTECTED; accept explicit-only DACLs with no inherited ACEs.
            if not protected and inherited_ace:
                raise OSError("DACL is not protected on %s" % path)
            if not protected and acl.AceCount == 0:
                raise OSError("DACL is not protected on %s" % path)
        finally:
            if sd:
                kernel32.LocalFree(sd)

    def _renforge_bridge_path_is_symlink(path):
        import os as _os
        import stat as _stat

        try:
            st = _os.lstat(path)
            if _stat.S_ISLNK(st.st_mode):
                return True
        except FileNotFoundError:
            return False
        except OSError:
            pass
        if _os.name == "nt":
            return _renforge_bridge_win_is_reparse(path)
        return False

    def _renforge_bridge_validate_control_dir(project_root):
        """Validate existing private control path without creating or repairing it."""
        import os as _os
        import stat as _stat

        control_dir = _renforge_bridge_control_dir(project_root)
        parent_dir = _os.path.dirname(control_dir)
        # Reject symlink/reparse ancestors before accepting control contents.
        if _renforge_bridge_path_is_symlink(parent_dir):
            raise OSError("control directory parent must not be a symlink")
        if _renforge_bridge_path_is_symlink(control_dir):
            raise OSError("control directory must not be a symlink")
        try:
            st = _os.lstat(control_dir)
        except OSError:
            raise
        if not _stat.S_ISDIR(st.st_mode):
            raise OSError("control directory is not a directory")
        if _os.name == "nt":
            _renforge_bridge_win_validate_protected_dacl(control_dir)
        else:
            if hasattr(_os, "geteuid") and st.st_uid != _os.geteuid():
                raise OSError("control directory is not owned by the current user")
            if (st.st_mode & 0o777) != 0o700:
                raise OSError("control directory mode must be 0700")
        return control_dir

    def _renforge_bridge_validate_private_file_stat(st, path):
        import os as _os
        import stat as _stat

        if not _stat.S_ISREG(st.st_mode):
            raise OSError("bridge info is not a regular file: %s" % path)
        if _os.name == "nt":
            _renforge_bridge_win_validate_protected_dacl(path)
        else:
            if hasattr(_os, "geteuid") and st.st_uid != _os.geteuid():
                raise OSError("bridge info is not owned by the current user: %s" % path)
            if (st.st_mode & 0o777) != 0o600:
                raise OSError("bridge info mode must be 0600: %s" % path)

    def _renforge_bridge_read_starting_info(project_root):
        """Read and validate the reserved starting bridge.json without following links."""
        import json as _json
        import os as _os
        import stat as _stat

        control_dir = _renforge_bridge_validate_control_dir(project_root)
        path = _renforge_bridge_info_path(project_root)
        if _renforge_bridge_path_is_symlink(path):
            raise OSError("bridge info must not be a symlink")

        if _os.name == "nt":
            handle = None
            try:
                handle = _renforge_bridge_win_create_file(
                    path,
                    0x80000000,
                    0x00000001,
                    3,
                    0x00200000,
                )
                if _renforge_bridge_win_get_file_type(handle) != 1:
                    raise OSError("bridge info is not a regular disk file: %s" % path)
                attrs = _renforge_bridge_win_get_handle_attributes(handle)
                if attrs & 0x400:
                    raise OSError("bridge info is a reparse point: %s" % path)
                _renforge_bridge_win_validate_protected_dacl(path)
                payload = _renforge_bridge_win_read_handle(handle, _RENFORGE_BRIDGE_INFO_MAX_BYTES + 1)
            finally:
                if handle is not None:
                    _renforge_bridge_win_close_handle(handle)
        else:
            try:
                st = _os.lstat(path)
            except OSError:
                raise
            _renforge_bridge_validate_private_file_stat(st, path)
            if st.st_size > _RENFORGE_BRIDGE_INFO_MAX_BYTES:
                raise OSError("bridge info exceeds size limit")

            flags = _os.O_RDONLY
            if hasattr(_os, "O_NOFOLLOW"):
                flags |= _os.O_NOFOLLOW
            if hasattr(_os, "O_CLOEXEC"):
                flags |= _os.O_CLOEXEC
            fd = _os.open(path, flags)
            try:
                opened = _os.fstat(fd)
                _renforge_bridge_validate_private_file_stat(opened, path)
                if opened.st_dev != st.st_dev or opened.st_ino != st.st_ino:
                    raise OSError("bridge info identity changed during open")
                payload = _os.read(fd, _RENFORGE_BRIDGE_INFO_MAX_BYTES + 1)
            finally:
                try:
                    _os.close(fd)
                except OSError:
                    pass

        if len(payload) > _RENFORGE_BRIDGE_INFO_MAX_BYTES:
            raise OSError("bridge info exceeds size limit")
        try:
            data = _json.loads(payload.decode("utf-8"))
        except Exception:
            raise OSError("bridge info is not valid JSON")
        if not isinstance(data, builtins.dict):
            raise OSError("bridge info must be an object")
        if set(data.keys()) != set(_RENFORGE_BRIDGE_INFO_KEYS):
            raise OSError("bridge info keys are invalid")
        sv = data.get("schema_version")
        pv = data.get("protocol_version")
        if type(sv) is not int or isinstance(sv, bool) or sv != 1 or type(pv) is not int or isinstance(pv, bool) or pv != 1:
            raise OSError("bridge info version is invalid")
        if data.get("state") != "starting":
            raise OSError("bridge info is not in starting state")
        if data.get("host") != "127.0.0.1":
            raise OSError("bridge info host is invalid")
        port = data.get("port")
        if type(port) is not int or isinstance(port, bool) or port != 0:
            raise OSError("bridge info starting port must be 0")
        session_id = data.get("session_id")
        token = data.get("token")
        recorded_root = data.get("project_root")
        if not _renforge_bridge_is_session_id(session_id):
            raise OSError("bridge info session_id is invalid")
        if not _renforge_bridge_is_token(token):
            raise OSError("bridge info token is invalid")
        if not isinstance(recorded_root, str) or not recorded_root:
            raise OSError("bridge info project_root is invalid")
        if recorded_root != project_root:
            raise OSError("bridge info project_root mismatch")
        return {
            "schema_version": 1,
            "protocol_version": 1,
            "state": "starting",
            "session_id": session_id,
            "project_root": recorded_root,
            "host": "127.0.0.1",
            "port": 0,
            "token": token,
        }

    def _renforge_bridge_fsync_directory(directory):
        import os as _os

        if not hasattr(_os, "O_RDONLY"):
            return
        flags = _os.O_RDONLY
        if hasattr(_os, "O_DIRECTORY"):
            flags |= _os.O_DIRECTORY
        try:
            fd = _os.open(directory, flags)
        except OSError:
            return
        try:
            try:
                _os.fsync(fd)
            except OSError:
                pass
        finally:
            try:
                _os.close(fd)
            except OSError:
                pass

    def _renforge_bridge_write_ready_info(project_root, payload):
        """Atomically publish ready bridge.json into an existing private control dir."""
        import json as _json
        import os as _os
        import secrets as _secrets
        import stat as _stat

        control_dir = _renforge_bridge_validate_control_dir(project_root)
        path = _renforge_bridge_info_path(project_root)
        encoded = _json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _RENFORGE_BRIDGE_INFO_MAX_BYTES:
            raise OSError("ready bridge info exceeds size limit")

        if _renforge_bridge_path_is_symlink(path):
            raise OSError("bridge info destination must not be a symlink")
        if _os.path.lexists(path):
            try:
                st = _os.lstat(path)
            except OSError:
                raise
            _renforge_bridge_validate_private_file_stat(st, path)

        if _os.name == "nt":
            # Overwrite the reserved starting record in place. Unlink/MoveFileEx
            # against a just-validated private file hits ERROR_SHARING_VIOLATION
            # on Windows CI (file still visible to the scanner / prior open).
            import time as _time

            if _renforge_bridge_path_is_symlink(path):
                raise OSError("bridge info destination must not be a symlink")
            if not _os.path.lexists(path):
                raise OSError("starting bridge info is missing")
            _renforge_bridge_win_validate_protected_dacl(path)

            # OPEN_EXISTING + shared access + retries: exclusive CREATE_ALWAYS
            # still races antivirus (errno/winerror 32) on GHA runners.
            handle = None
            last_error = None
            for _ in range(40):
                try:
                    handle = _renforge_bridge_win_create_file(
                        path,
                        0xC0000000,  # GENERIC_READ | GENERIC_WRITE
                        0x00000007,  # FILE_SHARE_READ|WRITE|DELETE
                        3,  # OPEN_EXISTING
                        0x00000080,  # FILE_ATTRIBUTE_NORMAL
                    )
                    break
                except OSError as exc:
                    last_error = exc
                    win_err = getattr(exc, "winerror", None)
                    if win_err is None and exc.args and isinstance(exc.args[0], int):
                        win_err = exc.args[0]
                    if win_err in (5, 32):
                        _time.sleep(0.025)
                        continue
                    raise
            if handle is None:
                raise OSError("failed to open starting bridge info for ready publish") from last_error
            try:
                if _renforge_bridge_win_get_file_type(handle) != 1:
                    raise OSError("bridge info is not a regular disk file")
                _renforge_bridge_win_truncate_handle(handle)
                _renforge_bridge_win_write_handle(handle, encoded)
                _renforge_bridge_win_flush_handle(handle)
            finally:
                _renforge_bridge_win_close_handle(handle)

            _renforge_bridge_win_set_protected_dacl(path)
            _renforge_bridge_win_validate_protected_dacl(path)
            if _renforge_bridge_win_is_reparse(path):
                raise OSError("published bridge info must not be a reparse point")
        else:
            flags = _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL
            if hasattr(_os, "O_NOFOLLOW"):
                flags |= _os.O_NOFOLLOW
            if hasattr(_os, "O_CLOEXEC"):
                flags |= _os.O_CLOEXEC

            temp_path = None
            fd = None
            last_error = None
            for _ in range(32):
                candidate = _os.path.join(
                    control_dir,
                    ".bridge.json.%s.tmp" % _secrets.token_hex(8),
                )
                try:
                    fd = _os.open(candidate, flags, 0o600)
                except FileExistsError as exc:
                    last_error = exc
                    continue
                except OSError as exc:
                    raise OSError("failed to create private temporary bridge info") from exc
                temp_path = candidate
                break
            if fd is None or temp_path is None:
                raise OSError("failed to allocate private temporary bridge info") from last_error

            try:
                opened = _os.fstat(fd)
                _renforge_bridge_validate_private_file_stat(opened, temp_path)
                written = 0
                while written < len(encoded):
                    chunk = _os.write(fd, encoded[written:])
                    if chunk <= 0:
                        raise OSError("short write while publishing bridge info")
                    written += chunk
                _os.fsync(fd)
                opened = _os.fstat(fd)
                _renforge_bridge_validate_private_file_stat(opened, temp_path)
                _os.close(fd)
                fd = None
                if _renforge_bridge_path_is_symlink(path):
                    raise OSError("bridge info destination must not be a symlink")
                _os.replace(temp_path, path)
                temp_path = None
                _renforge_bridge_fsync_directory(control_dir)
            finally:
                if fd is not None:
                    try:
                        _os.close(fd)
                    except OSError:
                        pass
                if temp_path is not None:
                    try:
                        if _os.path.lexists(temp_path) or _renforge_bridge_path_is_symlink(temp_path):
                            _os.unlink(temp_path)
                    except OSError:
                        pass

    def _renforge_publish_ready(bridge, port):
        """Validate reserved starting metadata and publish ready, or fail closed."""
        try:
            starting = _renforge_bridge_read_starting_info(bridge.project_root)
        except Exception:
            _renforge_bridge_startup_error(_RENFORGE_BRIDGE_STARTUP_INFO_CONFLICT)
            return False

        if (
            starting["session_id"] != bridge.session_id
            or starting["token"] != bridge.token
            or starting["project_root"] != bridge.project_root
            or starting["host"] != bridge.host
        ):
            _renforge_bridge_startup_error(_RENFORGE_BRIDGE_STARTUP_IDENTITY_MISMATCH)
            return False

        if type(port) is not int or isinstance(port, bool) or port < 1 or port > 65535:
            _renforge_bridge_startup_error(_RENFORGE_BRIDGE_STARTUP_PUBLICATION_FAILED)
            return False

        ready = {
            "schema_version": 1,
            "protocol_version": 1,
            "state": "ready",
            "session_id": bridge.session_id,
            "project_root": bridge.project_root,
            "host": "127.0.0.1",
            "port": port,
            "token": bridge.token,
        }
        try:
            _renforge_bridge_write_ready_info(bridge.project_root, ready)
        except Exception as exc:
            _renforge_bridge_startup_error(_RENFORGE_BRIDGE_STARTUP_PUBLICATION_FAILED)
            try:
                import sys as _sys
                detail = "%s: %s" % (type(exc).__name__, exc)
                # One line for CI logs; never include secrets (token is not in exc paths).
                _sys.stderr.write("RENFORGE_BRIDGE_STARTUP_DETAIL=%s\n" % detail.replace("\n", " ")[:500])
                _sys.stderr.flush()
            except Exception:
                pass
            return False
        return True

    def _renforge_listener(bridge):
        # The listener thread survives renpy.reload_script(), which restores
        # renpy.config from its post-import backup and *wipes the store* —
        # the __globals__ of init-python functions. A free-var reference to
        # ``socket`` then raises NameError when the 0.5s accept() timeout
        # fires ``except socket.timeout:``, silently killing the thread and
        # leaving the game running with a dead bridge. Local imports read
        # from sys.modules, which reload never touches, so the loop stays
        # alive across reloads. The helpers (_renforge_reply / publish /
        # _RenforgeRequest) are called inside the inner try/except, so a
        # transient NameError there is caught and only drops one connection;
        # they too use local imports for their stdlib references.
        import socket as _socket
        import json as _json
        server = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        published = False
        try:
            server.bind((bridge.host, bridge.port))
            bridge.port = server.getsockname()[1]
            # Accept connections before advertising a ready record. Publishing
            # first leaves a race where clients observe state="ready" but the
            # socket still rejects connections with ECONNREFUSED.
            server.listen(5)
            # Plain int only — never hang the bridge object off the store.
            setattr(renpy.store, "renforge_bridge_port", bridge.port)
            if not _renforge_publish_ready(bridge, bridge.port):
                bridge.stop.set()
                return
            published = True

            while not bridge.stop.is_set():
                try:
                    server.settimeout(0.5)
                    conn, _ = server.accept()
                except _socket.timeout:
                    continue
                except OSError:
                    # accept() can raise OSError (not just socket.timeout)
                    # on a recycled socket or a transient kernel error; never
                    # let it crash the loop the way a NameError on the bare
                    # ``socket`` name did before the local import.
                    continue

                try:
                    with conn:
                        # Bounded one-shot frame: 1 MiB max, 2s read budget.
                        conn.settimeout(2.0)
                        chunks = []
                        total = 0
                        max_bytes = 1 * 1024 * 1024
                        line = None
                        while True:
                            try:
                                piece = conn.recv(min(4096, max(1, max_bytes - total + 1)))
                            except Exception:
                                piece = b""
                            if not piece:
                                break
                            chunks.append(piece)
                            total += len(piece)
                            joined = b"".join(chunks)
                            if b"\n" in joined:
                                line = joined.split(b"\n", 1)[0]
                                break
                            if total > max_bytes:
                                _renforge_reply(conn, {"ok": False, "error": "authentication_failed"})
                                line = None
                                break
                        if not line:
                            continue
                        try:
                            msg = _json.loads(line.decode("utf-8"))
                        except Exception:
                            _renforge_reply(conn, {"ok": False, "error": "authentication_failed"})
                            continue
                        # builtins.dict: game code may shadow `dict` with a RevertableDict.
                        if not isinstance(msg, builtins.dict) or not isinstance(msg.get("token"), str):
                            _renforge_reply(conn, {"ok": False, "error": "authentication_failed"})
                            continue
                        provided = str(msg.get("token") or "")
                        expected = str(bridge.token or "")
                        try:
                            # Same-length only; mismatched lengths are invalid tokens.
                            token_ok = (
                                len(provided) == len(expected)
                                and _hmac.compare_digest(
                                    provided.encode("utf-8"),
                                    expected.encode("utf-8"),
                                )
                            )
                        except Exception:
                            token_ok = False
                        if not token_ok:
                            _renforge_reply(conn, {"ok": False, "error": "authentication_failed"})
                            continue

                        req = _RenforgeRequest(msg.get("command"), msg.get("payload"))
                        bridge.requests.put(req)
                        if req.event.wait(timeout=15.0):
                            if req.error is not None:
                                _renforge_reply(conn, {"error": req.error})
                            else:
                                _renforge_reply(conn, req.result)
                        else:
                            _renforge_reply(conn, {"error": "timeout_waiting_for_main_thread"})
                except Exception:
                    # One misbehaving connection — typically a client that
                    # timed out and hung up before the reply (the norm while
                    # reload_script blocks the main thread) — must never kill
                    # the accept loop and close the server socket with it.
                    continue
        except Exception:
            if not published:
                _renforge_bridge_startup_error(_RENFORGE_BRIDGE_STARTUP_PUBLICATION_FAILED)
                bridge.stop.set()
        finally:
            try:
                server.close()
            except Exception:
                pass

    def _renforge_install_callbacks(bridge):
        _renforge_progress_finish("cancelled")

        def _renforge_on_quit():
            _renforge_progress_finish("cancelled")

        def _renforge_on_label(name, abnormal):
            bridge.current_label = name
            bridge.push_event("label", {"label": name})
            if name in ("start", "_after_load", "_after_warp", "_start_replay"):
                bridge.pending_ending = None
                bridge.ending = None

        def _renforge_on_statement(name):
            if name != "return" and bridge.ending is None:
                return
            try:
                context = renpy.context()
                if (renpy.context_nesting_level() != 0
                        or getattr(renpy, "is_init_phase", lambda: False)()
                        or getattr(context, "init_phase", False)
                        or getattr(context, "_menu", False)
                        or getattr(context, "_main_menu", False)
                        or getattr(renpy.store, "main_menu", False)
                        or getattr(renpy.store, "_in_replay", False)):
                    return
                # Start(label) may target any game-defined label. Clear a
                # previous ending when root gameplay resumes, without relying
                # on a label named start or on the host seeing the first frame.
                bridge.ending = None
                if name != "return" or renpy.get_return_stack():
                    return
                filename, line = renpy.get_filename_line()
                bridge.pending_ending = {"reason": "script_return",
                                         "statement": {"file": filename, "line": line}}
            except Exception:
                # Missing lifecycle APIs must not break gameplay or guess an end.
                pass

        def _renforge_on_say(event, **kwargs):
            # Callbacks fire several times per line ("begin"/"show"/"end"); record
            # the text once, on the first event that carries it. 8.3+ passes
            # what. 8.0–8.2 do not, and the statement has already stored it.
            what = kwargs.get("what")
            if not isinstance(what, builtins.str) or not what:
                try:
                    stored = getattr(renpy.store, "_last_say_what", None)
                except Exception:
                    stored = None
                what = stored if isinstance(stored, builtins.str) else None
            if event == "begin":
                bridge.say_id += 1
                bridge.say_recorded = False
                # last_say stays this interaction's dialogue until the line
                # is over; say_stale_from is the first generation that is not.
                bridge.say_stale_from = None
            if event == "end":
                bridge.say_ended_generation = bridge.observe_generation
                # A line shown with interact=False (a menu's caption) stays up
                # for the next interaction; any other line ends with this one.
                held = kwargs.get("interact", True) is False
                bridge.say_stale_from = bridge.observe_generation + (2 if held else 1)
            if event == "interact_done" and kwargs.get("exception"):
                # Rollback or an error left the line without its "end".
                bridge.say_stale_from = bridge.observe_generation + 1
            if event in ("begin", "show") and what and not bridge.say_recorded:
                bridge.say_recorded = True
                previous_say = bridge.last_say
                what = _renforge_render_say_text(what)
                bridge.last_say = what
                who_expr = None
                try:
                    who_expr = getattr(renpy.store, "_last_say_who", None)
                except Exception:
                    who_expr = None
                who = _renforge_character_display_name(who_expr)
                if not isinstance(who, builtins.str):
                    who = None
                bridge.last_who = who
                bridge.push_event("say", {"what": what, "who": who, "say_id": bridge.say_id,
                                          "seen": _renforge_seen_current()})
                try:
                    prefs = getattr(renpy.store, "_preferences", None)
                    afm = bool(getattr(prefs, "afm_enable", False)) if prefs is not None else False
                except Exception:
                    afm = False
                if afm:
                    bridge.interaction_counter += 1
                    _renforge_emit_business(
                        "auto.advanced",
                        from_interaction=bridge.interaction_counter - 1,
                        to_interaction=bridge.interaction_counter,
                        previous_dialogue=previous_say,
                        dialogue=what,
                    )

        def _renforge_exception_handler(short_msg, full_msg, traceback_fn):
            bridge.pending_ending = None
            _renforge_progress_finish("crash")
            bridge.push_event("exception", {"short": short_msg, "full": full_msg})
            lines = [line.strip() for line in builtins.str(short_msg or "").splitlines() if line.strip()]
            bridge.last_exception = {"message": lines[-1] if lines else None,
                                     "traceback_file": traceback_fn if isinstance(traceback_fn, builtins.str) else None}
            previous = bridge.prev_exception_handler
            if callable(previous):
                return previous(short_msg, full_msg, traceback_fn)
            return False  # not handled: let Ren'Py show its normal error screen

        def _renforge_on_start_interact():
            # Once per ui.interact, before the redraw loop. restart_interaction
            # stays inside that call and must not change the observe generation.
            _renforge_ensure_after_load_hook()
            operation = bridge.progression
            if operation is not None and operation["active"]:
                # Freeze skip before the next interaction is constructed. The
                # periodic check resumes it only for known narrative.
                renpy.config.skipping = None
                if operation["enabled_generation"] == bridge.observe_generation:
                    operation["steps"] += 1
                    operation["enabled_generation"] = None
                if operation["steps"] >= operation["max_steps"]:
                    _renforge_progress_finish("max_steps")
            bridge.observe_generation = int(getattr(bridge, "observe_generation", 0)) + 1
            _renforge_stamp_screens()
            _renforge_commit_ending()
            if not getattr(bridge, "transitions_applied", False):
                # Preferences exist by the first interaction. The launcher asks
                # for this only when the session's preferences are isolated.
                bridge.transitions_applied = True
                import os as _os
                if _os.environ.get("RENFORGE_BRIDGE_TRANSITIONS") == "off":
                    prefs = getattr(renpy.store, "_preferences", None)
                    if prefs is not None and hasattr(prefs, "transitions"):
                        prefs.transitions = 0
            # A handler whose action opened a nested interaction (ShowMenu
            # outside the game menu, call_in_new_context) would not return
            # until that interaction ends. Reply now; observe shows its state.
            req = getattr(bridge, "nested_reply_request", None)
            if req is not None and not req.event.is_set():
                bridge.nested_reply_request = None
                req.result = {"ok": True, "nested_interaction": True, "command": req.command,
                              "note": "the action opened a nested interaction; observe to see it"}
                req.event.set()

        # 8.1+ runs the list. 8.0 runs the single ``label_callback`` and has
        # no list; assigning one would not be called.
        label_callbacks = getattr(renpy.config, "label_callbacks", None)
        # `list` in init python is RevertableList, so a config list fails that check.
        if isinstance(label_callbacks, builtins.list):
            label_callbacks.append(_renforge_on_label)
        elif hasattr(renpy.config, "label_callback"):
            previous_label = renpy.config.label_callback

            def _renforge_on_label_compat(name, abnormal=False):
                _renforge_on_label(name, abnormal)
                if callable(previous_label):
                    return previous_label(name, abnormal)

            renpy.config.label_callback = _renforge_on_label_compat
        renpy.config.all_character_callbacks.append(_renforge_on_say)
        statement_callbacks = getattr(renpy.config, "statement_callbacks", None)
        if statement_callbacks is not None and not any(
                getattr(callback, "__name__", "") == "_renforge_on_statement"
                for callback in statement_callbacks):
            statement_callbacks.append(_renforge_on_statement)
        bridge.prev_exception_handler = renpy.config.exception_handler
        renpy.config.exception_handler = _renforge_exception_handler
        renpy.config.periodic_callbacks.append(renforge_drain_bridge)
        start_callbacks = getattr(renpy.config, "start_interact_callbacks", None)
        if start_callbacks is None:
            start_callbacks = []
            renpy.config.start_interact_callbacks = start_callbacks
        already_observing = any(
            getattr(callback, "__name__", "") == "_renforge_on_start_interact"
            for callback in start_callbacks
        )
        if not already_observing:
            start_callbacks.append(_renforge_on_start_interact)
        quit_callbacks = getattr(renpy.config, "quit_callbacks", None)
        if quit_callbacks is None:
            quit_callbacks = []
            renpy.config.quit_callbacks = quit_callbacks
        if not any(getattr(callback, "__name__", "") == "_renforge_on_quit" for callback in quit_callbacks):
            quit_callbacks.append(_renforge_on_quit)
        _renforge_ensure_after_load_hook()

    def renforge_start_bridge():
        existing = getattr(_renforge_runtime, "bridge", None)
        if existing is not None:
            # renpy.reload_script() keeps the process — the listener thread,
            # its socket and this sys.modules entry all survive — but restores
            # renpy.config from its post-import backup before re-running this
            # init block. Every callback registered at first start is wiped
            # with it, so the bridge kept accepting connections that nothing
            # drained. Re-register on the fresh config and reuse the live
            # socket; never bind a second one.
            already_registered = any(
                getattr(callback, "__name__", "") == "renforge_drain_bridge"
                for callback in renpy.config.periodic_callbacks
            )
            if not already_registered:
                _renforge_install_callbacks(existing)
                setattr(renpy.store, "renforge_bridge_port", existing.port)
            return

        # Absent token means the launcher did not request a bridge — stay off.
        raw_token = os.environ.get("RENFORGE_BRIDGE_TOKEN")
        if raw_token is None:
            return

        token = "" if raw_token is None else str(raw_token).strip()
        session_id = os.environ.get("RENFORGE_BRIDGE_SESSION_ID", "")
        session_id = "" if session_id is None else str(session_id).strip()
        project_root = os.environ.get("RENFORGE_BRIDGE_PROJECT_ROOT", "")
        project_root = "" if project_root is None else str(project_root).strip()

        identity_ok = True
        if not _renforge_bridge_is_token(token) or not _renforge_bridge_is_session_id(session_id):
            identity_ok = False
        elif not project_root or not os.path.isabs(project_root):
            identity_ok = False
        else:
            try:
                if _renforge_bridge_path_is_symlink(project_root):
                    identity_ok = False
                elif not os.path.isdir(project_root):
                    identity_ok = False
                else:
                    canonical = os.path.realpath(project_root)
                    if canonical != project_root:
                        # Env must already be the canonical absolute root.
                        identity_ok = False
                    else:
                        project_root = canonical
            except OSError:
                identity_ok = False

        if not identity_ok:
            _renforge_bridge_startup_error(_RENFORGE_BRIDGE_STARTUP_IDENTITY_MISMATCH)
            return

        # Host is fixed to loopback; ignore any alternate host env.
        host = "127.0.0.1"
        try:
            port = int(os.environ.get("RENFORGE_BRIDGE_PORT", "0") or "0")
        except (TypeError, ValueError):
            port = 0
        if port < 0 or port > 65535:
            port = 0

        bridge = _RenforgeBridge(host, port, token, project_root, session_id)
        _renforge_runtime.bridge = bridge

        _renforge_install_callbacks(bridge)

        thread = threading.Thread(
            target=_renforge_listener,
            args=(bridge,),
            daemon=True,
            name="renforge.bridge.listener",
        )
        bridge.thread = thread
        thread.start()

    renforge_start_bridge()
