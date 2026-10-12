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
    import socket
    import struct
    import sys
    import threading
    # `time` is a store field. Games use that name for their own clock, which
    # replaces the module before a label callback runs.
    import time as _renforge_time
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

    class _RenforgeRequest(object):
        # NB: no __slots__ — Ren'Py forbids slotted classes in init python
        # (they are incompatible with its rollback machinery).
        def __init__(self, command, payload):
            self.command = command
            self.payload = payload
            self.event = threading.Event()
            self.result = None
            self.error = None

    class _RenforgeBridge(object):
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

    def _renforge_h_eval(payload):
        expr = (payload or {}).get("expr", "")
        value = eval(expr, {"__builtins__": __builtins__}, vars(renpy.store))
        return {"expr": expr, "value": _renforge_jsonable(value)}

    def _renforge_h_get_var(payload):
        name = (payload or {}).get("name")
        return {"name": name, "value": _renforge_jsonable(getattr(renpy.store, name))}

    def _renforge_h_set_var(payload):
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
        for letter in "abcdefghijklmnopqrstuvwxyz":
            attrs[letter] = "K_" + letter
        return attrs

    _RENFORGE_ACT_KEY_ATTRS = _renforge_act_key_attrs()
    _RENFORGE_ACT_KEY_LIST = "a-z, up, down, left, right, escape, return, space, tab"

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
        # Same limits Input.event applies while typing: allow, exclude, length.
        length = getattr(widget, "length", None)
        if isinstance(length, bool) or not isinstance(length, builtins.int) or length < 0:
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
        when this widget has no Ren'Py input value to replace.
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
            wrote = False
            try:
                update(filtered, True, True)
                wrote = True
            except TypeError:
                pass
            except Exception:
                pass
            if not wrote:
                try:
                    update(filtered, True)
                except Exception:
                    return False
            current = getattr(widget, "content", None)
            if current != filtered:
                return False
            return filtered
        value = getattr(widget, "value", None)
        set_text = getattr(value, "set_text", None) if value is not None else None
        if not callable(set_text):
            return None
        try:
            set_text(filtered)
        except Exception:
            return False
        try:
            widget.content = filtered
        except Exception:
            pass
        try:
            widget.caret_pos = len(filtered)
        except Exception:
            pass
        return filtered

    def _renforge_post_key(keycode, hold=None):
        phases = (pygame.KEYDOWN, pygame.KEYUP) if hold is None else ((pygame.KEYDOWN,) if hold else (pygame.KEYUP,))
        for phase in phases:
            pygame.event.post(pygame.event.Event(phase, {
                "key": keycode, "mod": getattr(pygame, "KMOD_NONE", 0), "unicode": "", "repeat": 0,
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

    def _renforge_h_save_slot(payload):
        payload = payload or {}
        slot = payload.get("slot")
        extra_info = payload.get("extra_info", "")
        if not isinstance(slot, str) or not slot.strip():
            return {"ok": False, "error": "save slot is required"}
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
        return {"ok": True, "slot": slot, "extra_info": extra_info}

    def _renforge_h_load_slot(payload):
        payload = payload or {}
        slot = payload.get("slot")
        if not isinstance(slot, str) or not slot.strip():
            return {"ok": False, "error": "save slot is required"}

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
            return {"ok": False, "error": "save slot not found: %s" % slot}

        load = getattr(renpy, "load", None)
        if not callable(load):
            return {"ok": False, "error": "save loading is unavailable"}

        def _do_load():
            try:
                load(slot)
            except Exception as exc:
                if _renforge_is_script_control_flow(exc):
                    # The engine restarts into the loaded game after the reply.
                    _renforge_emit_business("load.completed", slot=slot, restored_label=None)
                    setattr(exc, "renforge_result", {"ok": True, "slot": slot,
                        "restored_label": None, "restarting": True})
                raise

        _renforge_invoke(_do_load)
        bridge = _renforge_runtime.bridge
        restored_label = bridge.current_label if bridge is not None else None
        _renforge_emit_business(
            "load.completed",
            slot=slot,
            restored_label=restored_label,
        )
        return {"ok": True, "slot": slot, "restored_label": restored_label}

    def _renforge_h_list_slots(payload):
        payload = payload or {}
        regexp = payload.get("regexp")
        if regexp is not None and not isinstance(regexp, str):
            return {"ok": False, "error": "regexp must be a string"}

        list_slots = getattr(renpy, "list_slots", None)
        if not callable(list_slots):
            return {"ok": False, "error": "save slot listing is unavailable"}
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

        return {"ok": True, "slots": slots}

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
            else:
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

    def _renforge_center_point(element):
        center = element.get("center") or {}
        try:
            return int(center.get("x")), int(center.get("y")), True
        except (TypeError, ValueError, OverflowError):
            return 0, 0, False

    def _renforge_focus_screen(focus):
        return getattr(focus, "screen", None)

    def _renforge_full_modal(focus):
        """A screen whose modal flag grabs every point, the way IgnoreLayers does."""
        screen = _renforge_focus_screen(focus)
        modal = getattr(screen, "modal", None)
        if modal is None or callable(modal) or modal == "window":
            return False
        return bool(modal)

    def _renforge_modal_rank(focus, index, element=None):
        screen = _renforge_focus_screen(focus)
        zorder = getattr(screen, "zorder", None)
        try:
            zorder = int(zorder)
        except (TypeError, ValueError, OverflowError):
            zorder = 0
        layer = (element or {}).get("layer") or getattr(screen, "layer", None)
        try:
            layers = builtins.list(renpy.config.bottom_layers) + builtins.list(renpy.config.layers) + builtins.list(renpy.config.top_layers)
            layer_rank = layers.index(layer) if layer in layers else 0
        except Exception:
            layer_rank = 0
        return (layer_rank, zorder, index)

    def _renforge_same_screen(left, right):
        left_screen = _renforge_focus_screen(left)
        right_screen = _renforge_focus_screen(right)
        if left_screen is not None and right_screen is not None:
            return left_screen is right_screen
        left_name = _renforge_screen_name(left)
        right_name = _renforge_screen_name(right)
        return bool(left_name) and left_name == right_name

    def _renforge_top_modal(elements):
        winner = None
        rank = None
        for index, (focus, element) in enumerate(elements):
            if not _renforge_full_modal(focus):
                continue
            current = _renforge_modal_rank(focus, index, element)
            if rank is None or current > rank:
                rank = current
                winner = focus
        return winner

    def _renforge_modal_blocks(focus, modal, elements, element=None):
        if modal is None or _renforge_same_screen(focus, modal):
            return False
        modal_record = next((record for owner, record in elements if owner is modal), None)
        return (_renforge_modal_rank(focus, 0, element)[:2]
                <= _renforge_modal_rank(modal, 0, modal_record)[:2])

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

    def _renforge_point_candidates(bounds):
        if not bounds or bounds.get("width", 0) <= 0 or bounds.get("height", 0) <= 0:
            return []
        x, y, w, h = (bounds[key] for key in ("x", "y", "width", "height"))
        return [(int(x+min(w-1, w*px)), int(y+min(h-1, h*py)))
                for px, py in ((0.5, 0.5), (0.1, 0.1), (0.9, 0.1), (0.1, 0.9),
                               (0.9, 0.9), (0.5, 0.1), (0.5, 0.9), (0.1, 0.5), (0.9, 0.5))]


    def _renforge_engine_focus_ids():
        focus_module = getattr(renpy.display, "focus", None)
        return {id(getattr(focus, "widget", None))
                for focus in getattr(focus_module, "focus_list", ()) or ()}


    def _renforge_mark_coverage(elements):
        """Ask the engine's click hit test which controls a click would reach.

        ``focus_at_point`` applies layer order, modal screens, and focus masks.
        Without a completed render, the engine focus list, which already drops
        controls behind a full modal, is the remaining evidence.
        """
        focusable = _renforge_engine_focus_ids()
        top_modal = _renforge_top_modal(elements)
        for focus, element in elements:
            widget = getattr(focus, "widget", None)
            enabled = bool(element.get("enabled", True))
            if element.get("semantic"):
                # Semantic operations bypass the pointer, so a probe cannot
                # tell whether a modal screen above blocks them.
                blocked = _renforge_modal_blocks(focus, top_modal, elements, element)
                element["covered"] = blocked
                element["clickable"] = enabled and not blocked
                continue
            points = _renforge_point_candidates(element.get("bounds"))
            hits = [_renforge_render_hit(x, y) for x, y in points]
            point = next((p for p, hit in zip(points, hits)
                          if isinstance(hit, tuple) and hit[1] is widget), None)
            if point is not None:
                element["click_point"] = {"x": point[0], "y": point[1]}
                covered = False
            elif "modal" in hits:
                covered = True
            elif not hits or hits[0] == "unavailable":
                covered = False if widget is None or id(widget) in focusable else None
            elif not enabled:
                covered = False
            else:
                covered = None
                element["coverage_reason"] = "no eligible point proven by bounded focus probes"
            element["covered"] = covered
            element["clickable"] = enabled and element.get("visible") is not False and covered is False
        return elements


    def _renforge_focusable_elements(max_items=None, max_text_chars=None, inventory=None):
        pairs, _truncated = _renforge_observe_focus_pairs(inventory)
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

    def _renforge_focus_action_value(focus):
        # Button.per_interact copies a sensitive action onto clicked, and
        # Button.event runs clicked. Prefer that, then the declared action.
        menu_action = getattr(focus, "menu_action", None)
        if menu_action is not None:
            return menu_action
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

    def _renforge_click_focus(focus, element=None):
        """Click a focus center through shared synthetic input path."""
        fx = getattr(focus, "x", None)
        fy = getattr(focus, "y", None)
        fw = getattr(focus, "w", None)
        fh = getattr(focus, "h", None)
        if element is not None and element.get("bounds"):
            bounds = element["bounds"]
            x = int(bounds["x"] + bounds["width"] // 2)
            y = int(bounds["y"] + bounds["height"] // 2)
        elif fx is not None and fy is not None and fw and fh:
            x = int(fx + fw // 2)
            y = int(fy + fh // 2)
        else:
            find_position = getattr(getattr(renpy, "test", None), "testfocus", None)
            find_position = getattr(find_position, "find_position", None)
            if not callable(find_position):
                raise RuntimeError("Ren'Py focus position API is unavailable")
            px, py = find_position(focus, (None, None))
            x, y = int(px), int(py)

        if element is not None and element.get("click_point"):
            x, y = element["click_point"]["x"], element["click_point"]["y"]
        try:
            _renforge_click_pointer(x, y)
        except Exception as exc:
            if not _renforge_is_end_interaction(exc):
                raise
            setattr(exc, "renforge_pointer", (x, y))
            raise
        return x, y

    def _renforge_resolve_ui_element(payload, action):
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

        candidates = []
        for focus, element in _renforge_focusable_elements():
            if wanted_screen and str(element.get("screen") or "").casefold() != wanted_screen:
                continue
            if (wanted_id is not None and str(element.get("id")) != wanted_id
                    and element.get("widget_id") != wanted_id):
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
        focus, element, error = _renforge_resolve_ui_element(payload, "click_element")
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
            raw = _renforge_observe_raw(False, include_readout=False)
            refusal = _renforge_runtime.interaction.guard_identity(raw, raw["interaction"])
            if refusal:
                return {"ok": False, "error": refusal, "observation": raw}
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
        hit = _renforge_hit_stack(x, y)
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

    def _renforge_hit_stack(x, y):
        """Return the focus a click would hit, plus other rectangles at the point."""
        elements = _renforge_focusable_elements()
        hit = _renforge_render_hit(x, y)
        hits = [(focus, element) for focus, element in elements
                if element.get("covered") is not True
                and _renforge_bounds_contain(element.get("bounds") or {}, x, y)]
        topmost = hits[-1][1] if hits else None
        if hit == "modal":
            topmost = None
            hits = []
        elif isinstance(hit, tuple) and hit[0] == "displayable":
            for focus, element in elements:
                if getattr(focus, "widget", None) is hit[1]:
                    topmost = element
                    break
        underneath = []
        if topmost is not None:
            for _focus, element in reversed(hits):
                if element is not topmost:
                    underneath.append(element)
        result = {
            "ok": True,
            "x": x,
            "y": y,
            "coordinate_space": "logical",
            "topmost": topmost,
            "underneath": underneath,
        }
        if topmost is not None and underneath:
            result["warning"] = (
                "The intended button is covered by another interactive displayable."
                if False
                else "Multiple interactive displayables overlap this point."
            )
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

    def _renforge_move_mouse(focus):
        fx = getattr(focus, "x", None)
        fy = getattr(focus, "y", None)
        fw = getattr(focus, "w", None)
        fh = getattr(focus, "h", None)
        if fx is not None and fy is not None and fw and fh:
            x = int(fx + fw // 2)
            y = int(fy + fh // 2)
        else:
            find_position = getattr(getattr(renpy, "test", None), "testfocus", None)
            find_position = getattr(find_position, "find_position", None)
            if not callable(find_position):
                raise RuntimeError("Ren'Py focus position API is unavailable")
            px, py = find_position(focus, (None, None))
            x, y = int(px), int(py)

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
        try:
            x, y, method = _renforge_move_mouse(focus)
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
        focus, element, error = _renforge_resolve_ui_element(payload, "get_ui_element_bounds")
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
            inventory = _renforge_scene_inventory()
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
                    math = _renforge_runtime.interaction.math
                    x = max(bounds["x"], math.floor(min(p[0] for p in points)))
                    y = max(bounds["y"], math.floor(min(p[1] for p in points)))
                    right = min(bounds["x"]+bounds["width"], math.ceil(max(p[0] for p in points)))
                    bottom = min(bounds["y"]+bounds["height"], math.ceil(max(p[1] for p in points)))
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
        """Convert screenshot pixels through the same seam used by click_at."""
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
            return x, y, frame_data, "click_at point is outside the screenshot"
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
            if not _renforge_runtime.interaction.math.isfinite(x) or not _renforge_runtime.interaction.math.isfinite(y):
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
    _RENFORGE_SCENE_MAX_DEPTH = 48
    _RENFORGE_SCENE_MAX_TEXT_CHARS = 4096
    _RENFORGE_SCENE_MAX_ID_CHARS = 256
    _RENFORGE_SCENE_SEMANTIC_TYPES = ("image", "text", "button", "bar", "input", "imagemap", "hotspot")
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
        node["center"] = ({"x": bounds["x"] + bounds["width"] // 2,
                           "y": bounds["y"] + bounds["height"] // 2} if bounds else None)
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
        return out or None

    def _renforge_scene_overflow(d):
        return {"available": False, "reason": "natural text size is not established by completed render geometry"}

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
            if want_overflow and node.get("type") == "text":
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
        try:
            focusables = _renforge_focusable_elements(max_nodes + 1, max_text_chars, inventory)
        except Exception:
            focusables = []
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

        input_candidates, _unclassified = _renforge_input_candidates(inventory, focusables)
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

    def _renforge_observe_role(focus, widget):
        # Detect the concrete widget; the shared contract normalizes its role.
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
                return "/".join(parts)
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
                if index >= 400:
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
        # A camper is an object. fileName is the badge stem; name is the fallback.
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
        try:
            if widget.__class__.__name__ == "Drag":
                attrs = ("dragged", "clicked", "action")
        except Exception:
            attrs = ("action", "clicked")
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
        try:
            name = widget.__class__.__name__
        except Exception:
            name = ""
        if name == "Viewport":
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
        if widget is None:
            return None
        try:
            if widget.__class__.__name__ != "Drag":
                return None
        except Exception:
            return None
        name = getattr(widget, "drag_name", None)
        if isinstance(name, builtins.str) and name.strip():
            return name.strip()
        return None


    def _renforge_observe_drag_image(widget):
        if widget is None:
            return None
        try:
            if widget.__class__.__name__ != "Drag":
                return None
        except Exception:
            return None
        return _renforge_displayable_filename(getattr(widget, "child", None))


    def _renforge_observe_control_facts(widget):
        return {
            "drag_name": _renforge_observe_drag_name(widget),
            "alternate": _renforge_observe_action_attr(widget, "alternate"),
            "hovered": _renforge_observe_action_attr(widget, "hovered"),
            "selected": _renforge_observe_selected(widget),
            "adjustment": _renforge_observe_adjustment(widget),
        }


    def _renforge_display_name(who):
        # Character.name is the name drawn in the say window. The say callback
        # receives the Character object, not that string.
        if who is None:
            return None
        if isinstance(who, builtins.str):
            text = _renforge_render_say_text(who).strip()
            return text or None
        name = getattr(who, "name", None)
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


    def _renforge_character_display_name(who_expr):
        # ast.Say stores the who expression ("eileen"), not the display name.
        if who_expr is None:
            return None
        if not isinstance(who_expr, builtins.str):
            return _renforge_display_name(who_expr)
        ident = who_expr.strip()
        if not ident or not ident.replace("_", "a").isalnum() or ident[0].isdigit():
            return None
        try:
            character = getattr(renpy.store, ident, None)
        except Exception:
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
        try:
            return node.__class__.__name__ == "Input"
        except Exception:
            return False


    def _renforge_input_prompt(screen_node, widget):
        scope = getattr(screen_node, "scope", None) if screen_node is not None else None
        if isinstance(scope, builtins.dict):
            prompt = scope.get("prompt")
            if isinstance(prompt, builtins.str) and prompt.strip():
                return prompt.strip()
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
                record["center"] = {
                    "x": bounds["x"] + bounds["width"] // 2,
                    "y": bounds["y"] + bounds["height"] // 2,
                }
            records.append((widget, record))
        return records


    def _renforge_strip_say_tags(value):
        return _renforge_runtime.interaction.strip_text_tags(value)

    def _renforge_observe_dialogue(screen_nodes, say_dismiss):
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
        showing = scoped_what is not None or scoped_who_set or say_dismiss in (
            "dismiss",
            "dismiss_hard_pause",
        )
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
                if item_number >= 400 or len(entries) >= 400:
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
                entries.append((screen, index, str(caption), action))
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


    _RENFORGE_GAME_MENU_SCREENS = frozenset((
        None, "game_menu", "save", "load", "preferences", "history", "help", "about",
    ))


    def _renforge_action_list(action):
        if action is None:
            return []
        return builtins.list(action) if _renforge_observe_is_sequence(action) else [action]


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


    def _renforge_observe_focus_pairs(inventory=None):
        """Collect current controls once; legacy listings are views of these pairs."""
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
                    focus = types.SimpleNamespace(widget=widget, screen=context[2],
                        x=getattr(focus, "x", None), y=getattr(focus, "y", None),
                        w=getattr(focus, "w", None), h=getattr(focus, "h", None))
            screen = getattr(focus, "screen", None)
            for entry_number, (owner, index, caption, action) in enumerate(menu):
                if owner is screen and _renforge_action_contains(_renforge_focus_action_value(focus), action):
                    matched_menu.add(entry_number)
                    record["menu_index"] = index
                    record["text"] = caption
                    record["semantic"] = True
                    focus = types.SimpleNamespace(widget=widget, screen=owner, action=action,
                        menu_action=action, x=getattr(focus, "x", None), y=getattr(focus, "y", None),
                        w=getattr(focus, "w", None), h=getattr(focus, "h", None))
                    break
            record["widget_id"] = _renforge_observe_widget_id(focus, widget, inventory["named"])
            record["source"] = _renforge_source_location(widget)
            if _renforge_engine_chrome_action(_renforge_action_list(_renforge_focus_action_value(focus))):
                record["chrome"] = True
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
                bounds = record["bounds"]
                record["center"] = {"x": bounds["x"] + bounds["width"] // 2,
                                    "y": bounds["y"] + bounds["height"] // 2}
            pairs.append((focus, record))

        for ordinal, focus in enumerate(getattr(getattr(renpy.display, "focus", None), "focus_list", ())):
            if len(pairs) >= 400:
                focus_truncated = True
                break
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
            add(focus, {
                "screen": name, "role": role, "text": _renforge_focus_text(widget) or None,
                "enabled": bool(_renforge_focus_enabled(focus, widget)),
                "clickable": True, "covered": False, "visible": True,
                "image_name": _renforge_observe_image_name(widget) or _renforge_observe_drag_image(widget),
                "menu_index": None, "ordinal": ordinal,
                "action": _renforge_observe_action_class(widget), "bounds": bounds,
            })
        for widget, record in _renforge_observe_input_records(seen_widgets, inventory):
            add(types.SimpleNamespace(widget=widget, screen=contexts.get(id(widget), (None, None, None))[2]), record)
        for focus, record in _renforge_observe_kinetic_records(seen_widgets, inventory):
            add(focus, record)
        behavior = getattr(renpy.display, "behavior", None)
        button_type = getattr(behavior, "Button", None)
        if isinstance(button_type, type):
            for widget, layer, name, screen, _parents in inventory["nodes"]:
                if id(widget) in seen_widgets or not isinstance(widget, button_type):
                    continue
                geometry = _renforge_widget_geometry(widget, inventory)
                if geometry["visible"] is not True:
                    continue
                # Ren'Py leaves a Button out of the focus list when it has no
                # action (insensitive) or a modal screen hides it.
                focus = types.SimpleNamespace(widget=widget, screen=screen)
                add(focus, {"screen": name, "layer": layer, "role": "button",
                    "text": _renforge_focus_text(widget) or None,
                    "enabled": (getattr(widget, "clicked", None) is not None
                                and bool(_renforge_focus_enabled(focus, widget))),
                    "clickable": True, "covered": False,
                    "image_name": _renforge_observe_image_name(widget),
                    "menu_index": None, "ordinal": len(pairs),
                    "action": _renforge_observe_action_class(widget), **geometry})
        # A game's own displayable that holds a Ren'Py Action in ``action``
        # follows the engine's button convention: running the action is the click.
        action_type = getattr(getattr(renpy, "ui", None), "Action", None)
        sensitivity = getattr(behavior, "is_sensitive", None)
        if isinstance(action_type, type):
            for widget, layer, name, screen, _parents in inventory["nodes"]:
                if id(widget) in seen_widgets or type(widget).__module__.startswith("renpy."):
                    continue
                if not hasattr(widget, "action"):
                    continue
                actions = _renforge_action_list(widget.action)
                if not all(isinstance(item, action_type) for item in actions):
                    continue
                geometry = _renforge_widget_geometry(widget, inventory)
                if geometry["visible"] is not True:
                    continue
                # As with Ren'Py buttons, no action means insensitive.
                try:
                    enabled = bool(actions) and (bool(sensitivity(widget.action)) if callable(sensitivity) else True)
                except Exception:
                    enabled = True
                add(types.SimpleNamespace(widget=widget, screen=screen), {
                    "screen": name, "layer": layer, "role": "button",
                    "text": _renforge_focus_text(widget) or None,
                    "enabled": enabled, "clickable": enabled, "covered": False,
                    "semantic": True,
                    "image_name": _renforge_observe_image_name(widget),
                    "menu_index": None, "ordinal": len(pairs),
                    "action": _renforge_observe_action_class(widget), **geometry})
        for entry_number, (owner, index, caption, action) in enumerate(menu):
            if entry_number in matched_menu:
                continue
            sensitivity = getattr(behavior, "is_sensitive", None)
            enabled = bool(sensitivity(action)) if callable(sensitivity) else True
            name = _renforge_observe_screen_node_name(owner)
            focus = types.SimpleNamespace(widget=None, screen=owner, action=action, menu_action=action)
            pairs.append((focus, {"screen": name, "layer": getattr(owner, "layer", None),
                "role": "button", "type": "button", "text": caption,
                "enabled": enabled, "clickable": enabled, "covered": False,
                "visible": None, "bounds": None, "bounds_reason": "menu entry has no focus rectangle",
                "semantic": True, "menu_index": index, "ordinal": len(pairs),
                "action": action.__class__.__name__, "operations": ["click"],
                "coordinate_space": "logical"}))
        _renforge_mark_coverage(pairs)
        truncated = focus_truncated or len(pairs) > 400 or inventory.get("menu_truncated", False) or inventory["geometry"]["truncated"]
        pairs = pairs[:400]
        assigned = _renforge_runtime.interaction.assign_control_ids([record for _focus, record in pairs])
        for (_focus, record), identified in zip(pairs, assigned):
            record.update(identified)
        return pairs, truncated


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


    def _renforge_render_geometry(max_nodes=5000):
        """Read completed render occurrences without invoking displayable.render.

        Render.reverse maps children into their parent's coordinate space;
        child offsets are applied after that transform, as in take_focuses.
        The same widget may appear in several render paths.
        """
        render_mod = getattr(getattr(renpy, "display", None), "render", None)
        root = getattr(render_mod, "screen_render", None)
        width, height = _renforge_scene_window()
        result = {"occurrences": {}, "renders": {}, "available": root is not None, "truncated": False}
        if root is None:
            return result
        math = _renforge_runtime.interaction.math

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
            out = []
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
            corners = [point(matrix, 0, 0), point(matrix, w, 0),
                       point(matrix, w, h), point(matrix, 0, h)]
            if matrix[0]*matrix[3]-matrix[1]*matrix[2] < 0:
                corners.reverse()
            return corners

        drawn = {}

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

        initial = [(0.0, 0.0), (float(width), 0.0),
                   (float(width), float(height)), (0.0, float(height))]
        stack = [(root, (1, 0, 0, 1, 0, 0), initial, (), (), None)]
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
                if not all(math.isfinite(v) for p in corners for v in p):
                    raise ValueError("non-finite geometry")
                polygon = corners
                for index, edge in enumerate(clip):
                    polygon = clip_polygon(polygon, edge, clip[(index+1) % len(clip)])
                if not clip or getattr(render, "alpha", 1) <= 0 or w <= 0 or h <= 0:
                    polygon = []
                if polygon and not reason:
                    x = math.floor(min(p[0] for p in polygon))
                    y = math.floor(min(p[1] for p in polygon))
                    right = math.ceil(max(p[0] for p in polygon))
                    bottom = math.ceil(max(p[1] for p in polygon))
                    bounds = {"x": x, "y": y, "width": right-x, "height": bottom-y}
                    visible = bounds["width"] > 0 and bounds["height"] > 0
                else:
                    bounds, visible = None, None if reason else False
            except Exception:
                bounds, visible, reason = None, None, "unsupported render geometry"
                corners = []
            empty = None
            if visible and not draws(render):
                # A full-frame canvas that blits nothing is not on screen. The
                # reason describes this render only; children keep their geometry.
                bounds, visible, empty = None, False, "renders nothing"
            occurrence = {"bounds": bounds, "visible": visible,
                          "bounds_reason": reason or empty or (None if visible else "clipped, transparent, or zero-sized"),
                          "render_path": builtins.list(path)}
            for widget in getattr(render, "render_of", ()) or ():
                result["occurrences"].setdefault(id(widget), []).append(dict(occurrence))
                result["renders"].setdefault(id(widget), []).append((render, matrix, occurrence))
            child_clip = clip
            if corners and not reason:
                xclip = bool(getattr(render, "xclipping", False))
                yclip = bool(getattr(render, "yclipping", False))
                if xclip or yclip:
                    child_clip = builtins.list(clip)
                    # Use local edge indices before orientation normalization.
                    raw = [point(matrix, 0, 0), point(matrix, w, 0),
                           point(matrix, w, h), point(matrix, 0, h)]
                    for index in ((0, 2) if yclip else ()) + ((1, 3) if xclip else ()):
                        start, end = raw[index], raw[(index+1) % 4]
                        if matrix[0]*matrix[3]-matrix[1]*matrix[2] < 0:
                            start, end = end, start
                        child_clip = clip_polygon(child_clip, start, end)
            if getattr(render, "alpha", 1) <= 0:
                child_clip = []
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
        nodes = []
        seen = set()
        named = {}
        stack = [(root, layer, None, None, ()) for layer, root in reversed(_renforge_scene_roots())]
        while stack and len(nodes) < 5000:
            node, layer, screen_name, screen_node, parents = stack.pop()
            if node is None or id(node) in seen or getattr(node, "hiding", False):
                continue
            seen.add(id(node))
            class_name = node.__class__.__name__
            children = []
            extra_widgets = []
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
            for attr in ("child", "raw_child"):
                child = getattr(node, attr, None)
                if child is not None:
                    children.append(child)
            children.extend(extra_widgets)
            lineage = parents + (id(node),)
            for child in reversed(children):
                stack.append((child, layer, screen_name, screen_node, lineage))
        return {"nodes": nodes, "named": named, "truncated": bool(stack), "geometry": _renforge_render_geometry()}


    def _renforge_collect_interaction(inventory=None):
        if inventory is None:
            inventory = _renforge_scene_inventory()
        screens = []
        screen_nodes = []
        seen_screens = {}
        dismiss = []
        for node, layer, _name, _screen, _parents in inventory["nodes"]:
            class_name = node.__class__.__name__
            if class_name == "SayBehavior":
                for attr in ("dismiss", "dismiss_unfocused"):
                    dismiss.extend(_renforge_observe_event_names(getattr(node, attr, None)))
            if class_name == "ScreenDisplayable":
                _renforge_observe_record_screen(node, layer, screens, seen_screens)
                screen_nodes.append(node)
        say_dismiss = _renforge_runtime.interaction.normalize_say_dismiss(dismiss)
        return screens, say_dismiss, inventory["truncated"], screen_nodes

    def _renforge_observe_record_screen(node, layer_name, screens, seen_screens):
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
                            if get_time() - started < delay + _RENFORGE_PAUSE_GRACE_SECONDS:
                                return True
                        except Exception:
                            pass
            if depth < 3:
                for child in getattr(render, "children", ()) or ():
                    target = child[0] if child else None
                    if hasattr(target, "render_of"):
                        stack.append((target, depth + 1))
        return False

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
        if _renforge_short_pause_pending():
            return False, "pause"
        return True, None


    def _renforge_game_lifecycle():
        # Use engine context flags, never game-defined screen/label names.
        bridge = _renforge_runtime.bridge
        try:
            context = renpy.context()
            main_menu = getattr(context, "_main_menu", None)
            if not isinstance(main_menu, bool):
                main_menu = getattr(renpy.store, "main_menu", None)
        except Exception:
            main_menu = None
        if not isinstance(main_menu, bool):
            return {"phase": "unknown", "ending": None}
        if main_menu and bridge is not None and bridge.pending_ending is not None:
            bridge.ending_counter += 1
            bridge.ending = dict(bridge.pending_ending, id=bridge.ending_counter)
            bridge.pending_ending = None
            bridge.push_event("ending", bridge.ending)
        ending = bridge.ending if main_menu and bridge is not None else None
        return {"phase": "ended" if ending else "main_menu" if main_menu else "playing",
                "ending": ending}

    def _renforge_input_candidates(inventory, pairs):
        """Report opaque event implementations without interpreting game code."""
        handled = {id(getattr(focus, "widget", None)) for focus, record in pairs if record.get("operations")}
        top_modal = _renforge_top_modal(pairs)
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
            blocked = _renforge_modal_blocks(types.SimpleNamespace(screen=screen),
                top_modal, pairs, {"layer": layer})
            # A handler that draws at most one pixel cannot be pointed at: it
            # listens for keys, and dialogue still advances past it.
            bounds = geometry["bounds"] or {}
            pointer = not (geometry["visible"] is True and
                           bounds.get("width", 0) * bounds.get("height", 0) <= 1)
            relevant = geometry["visible"] is not False and not blocked and pointer
            unclassified = unclassified or relevant
            if len(candidates) >= 64:
                unclassified = True
                break
            if geometry["visible"] is False:
                # Kept so agents know the handler exists; it is not on screen.
                candidates.append({"screen": name, "layer": layer,
                    "source": _renforge_source_location(node),
                    "class": "%s.%s" % (type(node).__module__, type(node).__name__),
                    "visible": False, "bounds": None, "bounds_reason": geometry["bounds_reason"]})
                continue
            candidates.append({"screen": name, "layer": layer,
                "source": _renforge_source_location(node),
                "class": "%s.%s" % (type(node).__module__, type(node).__name__),
                "pointer": pointer,
                "reason": "custom event handler; input semantics and eligibility unknown",
                "bounds": geometry["bounds"], "bounds_reason": geometry["bounds_reason"],
                "visible": geometry["visible"], "modal_blocked": blocked,
                "occurrences": inventory["geometry"]["occurrences"].get(id(node), [])})
        return candidates, unclassified


    def _renforge_observe_raw(screenshot, with_pairs=False, include_readout=True):
        bridge = _renforge_runtime.bridge
        interaction = 0
        label = None
        if bridge is not None:
            interaction = int(getattr(bridge, "observe_generation", 0) or 0)
            label = bridge.current_label if isinstance(bridge.current_label, str) else None
        stable, reason = _renforge_observe_stability()
        inventory = _renforge_scene_inventory()
        screens, say_dismiss, walk_truncated, screen_nodes = _renforge_collect_interaction(inventory)
        # An interaction can start before its first draw. Never attach the
        # previous screen's capture/geometry to a new actionable observation.
        if inventory["geometry"]["available"] and any(
                id(screen) not in inventory["geometry"]["occurrences"] for screen in screen_nodes):
            stable, reason = False, "not_ready"
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
            if widget is not None:
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
    _RENFORGE_EXPLAIN_KEYWORDS = ("action", "sensitive", "hovered", "unhovered", "selected", "alternate", "value", "id")


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
        state["entries"].append({
            "depth": depth,
            "kind": kind,
            "file": where.get("file"),
            "line": where.get("line"),
            "text": _renforge_explain_text(text),
        })


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
                emit(state, depth, "choice", loc, text)
                _renforge_explain_label_walk(block, depth + 1, state)
        elif kind == "If":
            for index, entry in enumerate(getattr(node, "entries", None) or ()):
                branch, cond_text = _renforge_explain_branch(index, entry[0], "if")
                emit(state, depth, branch, loc, cond_text)
                _renforge_explain_label_walk(entry[1], depth + 1, state)
        elif kind == "While":
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
                "parameters": _renforge_explain_str(params) if params else None,
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
        state = {"entries": [], "limit": limit, "truncated": False}
        _renforge_explain_label_walk(getattr(node, "block", None), 0, state)
        return {
            "ok": True,
            "label": name,
            "file": where.get("file"),
            "line": where.get("line"),
            "statements": state["entries"],
            "truncated": state["truncated"],
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
                # A branch can set its parent's properties, such as a button's action.
                keywords = _renforge_explain_sl_text(types.SimpleNamespace(
                    keyword=getattr(entry[1], "keyword", None)))
                emit(state, depth, branch, loc, (cond_text + " : " + keywords) if keywords else cond_text)
                _renforge_explain_screen_walk(getattr(entry[1], "children", None), depth + 1, state)
        elif kind == "SLFor":
            text = "%s in %s" % (_renforge_explain_str(getattr(node, "variable", None)),
                                 _renforge_explain_str(getattr(node, "expression", None)))
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
            "parameters": _renforge_explain_str(getattr(screen, "parameters", None)) or None,
        }
        ast = getattr(screen, "ast", None)
        if ast is None:
            head.update({"nodes": [], "truncated": False, "reason": "screen defined in Python"})
            return head
        state = {"entries": [], "limit": limit, "truncated": False}
        _renforge_explain_screen_walk(getattr(ast, "children", None), 0, state)
        head.update({"nodes": state["entries"], "truncated": state["truncated"]})
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
        summary = {
            "id": element.get("id"),
            "text": element.get("text"),
            "screen": element.get("screen"),
            "source": element.get("source"),
        }
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
        for node in getattr(script, "all_stmts", None) or ():
            where = _renforge_explain_loc(node)
            if where is None or (where["file"], where["line"]) != wanted:
                continue
            texts = _renforge_explain_node_texts(node)
            found.append({"kind": node.__class__.__name__,
                          "text": "\n".join(text for _kind, text in texts)[:4000]})
            if len(found) >= 8:
                break
        if not found:
            return None
        return {"statement": None, "script": found}


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


    def _renforge_explain_search(needle, limit):
        """Every loaded statement whose text contains ``needle``, with its enclosing label."""
        wanted = needle.casefold()
        labels = {}
        for key, value in _renforge_explain_namemap().items():
            if value.__class__.__name__ == "Label":
                where = _renforge_explain_loc(value)
                name = key if isinstance(key, builtins.str) else getattr(value, "name", None)
                if where and isinstance(name, builtins.str):
                    labels.setdefault(where["file"], []).append((where["line"], name))
        for entries in labels.values():
            entries.sort()
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
            enclosing = None
            for line, name in labels.get(where.get("file"), ()):
                if line > (where.get("line") or 0):
                    break
                enclosing = name
            kind, text = found[0]
            at = text.casefold().find(wanted)
            snippet = text[max(0, at - 60):at + len(needle) + 60]
            matches.append({"kind": kind, "file": where.get("file"), "line": where.get("line"),
                            "label": enclosing, "text": _renforge_explain_text(snippet)})
        # Screen language: displayable arguments, properties, and conditions.
        seen = set()
        for screen_name, _variant, screen in _renforge_explain_screens():
            ast = getattr(screen, "ast", None)
            if truncated or ast is None or id(ast) in seen:
                continue
            seen.add(id(ast))
            stack = builtins.list(getattr(ast, "children", None) or ())
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
                    for _label, children in _renforge_explain_branches(node):
                        stack.extend(children or ())
                except Exception:
                    continue
                if hit is None:
                    continue
                if len(matches) >= limit:
                    truncated = True
                    break
                where = _renforge_explain_loc(node) or {}
                at = hit.casefold().find(wanted)
                matches.append({"kind": "screen", "file": where.get("file"), "line": where.get("line"),
                                "screen": screen_name,
                                "text": _renforge_explain_text(hit[max(0, at - 60):at + len(needle) + 60])})
        return {"ok": True, "search": needle, "matches": matches, "truncated": truncated}


    def _renforge_h_explain(payload):
        payload = payload or {}
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
        payload = payload or {}
        raw = _renforge_observe_raw(False, include_readout=False)
        snapshot = _renforge_runtime.interaction.classify(raw)
        refusal = _renforge_runtime.interaction.guard_dismiss(
            snapshot, payload.get("interaction"), session_id=payload.get("session_id"),
            observation_id=payload.get("observation_id"))
        if refusal:
            return {"ok": False, "error": refusal, "interaction": raw["interaction"]}
        renpy.exports.queue_event("dismiss")
        return {"ok": True, "interaction": raw["interaction"]}

    def _renforge_progress_public(operation):
        return {key: operation[key] for key in ("id", "active", "stop", "steps")}

    _RENFORGE_PROGRESS_PERIODIC_MS = 5


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
        prefs = operation["preferences"]
        for name, value in operation["saved_preferences"].items():
            setattr(prefs, name, value)

    def _renforge_progress_tick():
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
        raw = _renforge_observe_raw(False, include_readout=False)
        if not raw["stable"]:
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
            if not getattr(renpy.config, "allow_skipping", True) or not getattr(renpy.store, "_skipping", True):
                _renforge_progress_finish("unsupported")
                return
            operation["enabled_generation"] = bridge.observe_generation
            # Save the rendered copy before the engine moves to another line.
            dialogue = raw.get("dialogue")
            if dialogue:
                for event in reversed(bridge.events):
                    if event.get("type") == "say" and event.get("say_id") == bridge.say_id:
                        event.update(dialogue)
                        break
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
        payload = payload or {}
        bridge = _renforge_runtime.bridge
        operation = bridge.progression
        operation_id = payload.get("id")
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
        if not _renforge_runtime.interaction._real_number(timeout) or not 0.05 <= timeout <= 120:
            return {"ok": False, "error": "invalid_budget"}
        expires_at = payload.get("expires_at")
        if expires_at is not None:
            if not _renforge_runtime.interaction._real_number(expires_at):
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
        }
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
        record["alternate"] = facts.get("alternate")
        record["hovered"] = facts.get("hovered")
        record["selected"] = facts.get("selected")
        record["adjustment"] = facts.get("adjustment")
        role = _renforge_observe_normalize_role(record.get("role"))
        operations = _renforge_observe_operations(role)
        if role == "bar":
            adjustment = getattr(widget, "adjustment", None)
            operations = ["value"] if callable(getattr(adjustment, "change", None)) and getattr(adjustment, "adjustable", True) is not False else []
        elif role == "viewport":
            operations = ["value"] if any(callable(getattr(getattr(widget, attr, None), "change", None)) for attr in ("xadjustment", "yadjustment")) else []
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
        if node is None:
            return None
        try:
            name = node.__class__.__name__
        except Exception:
            return None
        if name == "Bar":
            return "bar"
        if name == "Viewport":
            return "viewport"
        if name == "Drag":
            return "drag"
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
                "text": _renforge_focus_text(widget) or None,
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


    def _renforge_act_value(payload, focus, wanted, interaction, control):
        widget = getattr(focus, "widget", None)
        result = {
            "ok": True,
            "operation": "value",
            "id": wanted,
            "interaction": interaction,
            "screen": control.get("screen"),
            "via": "action",
        }
        if control.get("role") == "viewport":
            for axis, field in (("x", "xadjustment"), ("y", "yadjustment")):
                if axis in payload:
                    number = payload[axis]
                    if not _renforge_runtime.interaction._real_number(number):
                        return {"ok": False, "error": "value_required", "interaction": interaction, "id": wanted}
                    if not callable(getattr(getattr(widget, field, None), "change", None)):
                        return {"ok": False, "error": "unsupported", "interaction": interaction, "id": wanted}
            wrote = False
            for axis, field in (("x", "xadjustment"), ("y", "yadjustment")):
                if axis not in payload:
                    continue
                number = payload.get(axis)
                if isinstance(number, bool) or not isinstance(number, (builtins.int, builtins.float)):
                    return {
                        "ok": False,
                        "error": "value_required",
                        "interaction": interaction,
                        "id": wanted,
                    }
                changed, stored = _renforge_change_adjustment(getattr(widget, field, None), number)
                if not changed:
                    return {
                        "ok": False,
                        "error": "unsupported",
                        "interaction": interaction,
                        "id": wanted,
                    }
                result[axis] = stored
                wrote = True
            if not wrote:
                return {
                    "ok": False,
                    "error": "value_required",
                    "interaction": interaction,
                    "id": wanted,
                }
            return result
        number = payload.get("value") if "value" in payload else None
        if isinstance(number, bool) or not isinstance(number, (builtins.int, builtins.float)):
            return {"ok": False, "error": "value_required", "interaction": interaction, "id": wanted}
        changed, stored = _renforge_change_adjustment(getattr(widget, "adjustment", None), number)
        if not changed:
            return {"ok": False, "error": "unsupported", "interaction": interaction, "id": wanted}
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


    def _renforge_act_drop(payload, focus, wanted, interaction, control, pairs):
        widget = getattr(focus, "widget", None)
        result = {
            "ok": True,
            "operation": "drop",
            "id": wanted,
            "interaction": interaction,
            "screen": control.get("screen"),
            "action": control.get("action"),
            "via": "action",
        }
        if "drop" not in payload:
            clicked = getattr(widget, "clicked", None)
            if clicked is not None:
                if not _renforge_invoke_focus_action(focus):
                    return {
                        "ok": False,
                        "error": "unsupported",
                        "interaction": interaction,
                        "id": wanted,
                    }
                result["operation"] = "click"
                return result
            payload = dict(payload)
            payload["drop"] = ""
        drop_name = payload.get("drop")
        target = None
        if isinstance(drop_name, builtins.str) and drop_name:
            matched = False
            for other_focus, other in pairs:
                if other.get("id") != drop_name:
                    continue
                target = getattr(other_focus, "widget", None)
                if other.get("role") != "drag" or not other.get("droppable") or not other.get("enabled", True) or other.get("covered"):
                    return {"ok": False, "error": "unsupported", "interaction": interaction, "id": drop_name}
                drop_allowable = getattr(target, "drop_allowable", None)
                if callable(drop_allowable) and not drop_allowable(target, _renforge_drag_joined(widget)):
                    return {"ok": False, "error": "unsupported", "interaction": interaction, "id": drop_name}
                result["drop"] = drop_name
                matched = True
                break
            if not matched:
                return {"ok": False, "error": "missing", "interaction": interaction, "id": drop_name}
        else:
            result["drop"] = None
        dragged = getattr(widget, "dragged", None)
        dropped = getattr(target, "dropped", None) if target is not None else None
        if dragged is None and dropped is None:
            return {"ok": False, "error": "unsupported", "interaction": interaction, "id": wanted}
        joined = _renforge_drag_joined(widget)
        rv = None
        if dragged is not None:
            called, rv = _renforge_call_action(dragged, (joined, target))
            if not called:
                return {"ok": False, "error": "unsupported", "interaction": interaction, "id": wanted}
        if rv is None and target is not None:
            dropped = getattr(target, "dropped", None)
            if dropped is not None:
                called, rv = _renforge_call_action(dropped, (target, joined))
                if not called:
                    return {
                        "ok": False,
                        "error": "unsupported",
                        "interaction": interaction,
                        "id": wanted,
                    }
        _renforge_end_action_result(rv)
        return result


    def _renforge_h_act(payload):
        payload = payload or {}
        contract = _renforge_runtime.interaction
        refusal = contract.validate_act_payload(payload)
        if refusal:
            return {"ok": False, "error": refusal}
        raw, pairs = _renforge_observe_raw(False, with_pairs=True, include_readout=False)
        interaction = raw["interaction"]
        bridge = _renforge_runtime.bridge
        key = payload.get("key")
        releasing = (payload.get("hold") is False and isinstance(key, builtins.str)
                     and key.strip().casefold() in getattr(bridge, "held_keys", set()))
        # A key we hold must be releasable after it changes the interaction.
        # Session ownership still applies; unrelated key-up requests are guarded.
        if releasing:
            if payload.get("session_id") is not None and payload["session_id"] != raw["session_id"]:
                return {"ok": False, "error": "stale", "interaction": interaction}
        else:
            refusal = contract.guard_identity(
                raw, payload.get("interaction"), session_id=payload.get("session_id"),
                observation_id=payload.get("observation_id"))
            if refusal:
                return {"ok": False, "error": refusal, "interaction": interaction, "observation": raw}
        key_value = payload.get("key") if "key" in payload else None
        if key_value is not None:
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
                    return {"ok": False, "error": "unsupported", "interaction": interaction}
            else:
                hold = None
            event_names = _renforge_post_key(keycode, hold)
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
        wanted = payload.get("id")
        if not isinstance(wanted, builtins.str) or not wanted:
            return {"ok": False, "error": "missing", "interaction": interaction}
        match = next(((focus, control) for focus, control in pairs if control.get("id") == wanted), None)
        if match is None:
            return {"ok": False, "error": "missing", "interaction": interaction, "id": wanted}
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
        if "value" in operations:
            try:
                return _renforge_with_focus_screen(focus, lambda: _renforge_act_value(payload, focus, wanted, interaction, control))
            except Exception as exc:
                if not _renforge_is_script_control_flow(exc):
                    raise
                result = {
                    "ok": True,
                    "operation": "value",
                    "id": wanted,
                    "interaction": interaction,
                    "screen": control.get("screen"),
                    "via": "action",
                }
                setattr(exc, "renforge_result", result)
                raise
        if "drop" in operations:
            try:
                return _renforge_with_focus_screen(focus, lambda: _renforge_act_drop(payload, focus, wanted, interaction, control, pairs))
            except Exception as exc:
                if not _renforge_is_script_control_flow(exc):
                    raise
                result = {
                    "ok": True,
                    "operation": "drop",
                    "id": wanted,
                    "interaction": interaction,
                    "screen": control.get("screen"),
                    "action": control.get("action"),
                    "via": "action",
                }
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
                return {"ok": False, "error": "text_required", "interaction": interaction, "id": wanted}
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
        return {"ok": False, "error": "unsupported", "interaction": interaction, "id": wanted}

    _RENFORGE_HANDLERS = {
        "ping": _renforge_h_ping,
        "get_state": _renforge_h_get_state,
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
        if prev_skip and not skipping:
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
        # RenForge keeps its state outside the game; the launcher says where.
        import os as _os
        configured = (_os.environ.get("RENFORGE_BRIDGE_CONTROL_DIR") or "").strip()
        if configured:
            return configured
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
        renforge_dir = _os.path.dirname(control_dir)
        # Reject symlink/reparse ancestors before accepting control contents.
        if _renforge_bridge_path_is_symlink(renforge_dir):
            raise OSError("renforge directory must not be a symlink")
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
            if event == "end":
                bridge.say_ended_generation = bridge.observe_generation
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
                bridge.push_event("say", {"what": what, "who": who, "say_id": bridge.say_id})
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
            previous = bridge.prev_exception_handler
            if callable(previous):
                return previous(short_msg, full_msg, traceback_fn)
            return False  # not handled: let Ren'Py show its normal error screen

        def _renforge_on_start_interact():
            # Once per ui.interact, before the redraw loop. restart_interaction
            # stays inside that call and must not change the observe generation.
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
            if not getattr(bridge, "transitions_applied", False):
                # Preferences exist by the first interaction. The launcher asks
                # for this only when the session's preferences are isolated.
                bridge.transitions_applied = True
                if os.environ.get("RENFORGE_BRIDGE_TRANSITIONS") == "off":
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
