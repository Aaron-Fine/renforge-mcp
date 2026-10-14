#!/usr/bin/env python3
"""Capture normalized bridge ``observe`` replies for a fixture game, per Ren'Py version.

Use before and after a controls refactor, then compare:

    .venv/bin/python scripts/capture_observe_baseline.py --out DIR
    .venv/bin/python scripts/capture_observe_baseline.py --out DIR2 --check DIR
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from renforge.bridge.launcher import launch_with_bridge
from renforge.project import RenpyProject
from renforge.sdk import get_or_install_sdk

DEFAULT_VERSIONS = ("8.5.3", "8.0.3")

FIXTURE_SCRIPT = '''
screen say(who, what):
    vbox:
        if who:
            text who id "who"
        text what id "what"

screen choice(items):
    vbox:
        for item in items:
            textbutton item.caption action item.action

default rf_count = 0

transform rf_half:
    zoom 0.5

screen rf_hub():
    textbutton "Enabled" action SetVariable("rf_count", rf_count + 1) pos (20, 20)
    textbutton "Disabled" action None pos (20, 80)
    textbutton "Gated" action SetVariable("rf_count", 0) sensitive False pos (20, 140)
    imagebutton idle Solid("#f00", xysize=(80, 40)) hover Solid("#0f0", xysize=(80, 40)) action NullAction() pos (300, 20)
    bar value VariableValue("rf_count", 10) pos (300, 100) xsize 200 ysize 20
    viewport:
        pos (600, 20)
        xysize (200, 100)
        mousewheel True
        vbox:
            for i in range(10):
                textbutton "Row [i]" action NullAction()
    frame at rf_half:
        pos (20, 300)
        textbutton "Zoomed" action NullAction()

screen rf_modal():
    modal True
    zorder 100
    textbutton "Close" action Hide("rf_modal") align (0.5, 0.5)

label main_menu:
    return

label start:
    "Line one."
    menu:
        "Pick A":
            pass
        "Pick B":
            pass
    call screen rf_hub
    return
'''

DROP_KEYS = frozenset({
    "observation_id",
    "session_id",
    "event_seq",
    "frame",
    "frame_id",
    "frame_hash",
    "sha256",
    "screenshot_base64",
    "interaction",
    "say_id",
    "progression",
})

POLL_SECONDS = 15.0
POLL_INTERVAL = 0.1


def normalize(obj: Any) -> Any:
    """Recursively drop nondeterministic keys from dicts."""
    if isinstance(obj, dict):
        return {key: normalize(value) for key, value in obj.items() if key not in DROP_KEYS}
    if isinstance(obj, list):
        return [normalize(item) for item in obj]
    return obj


def iter_elements(obj: Any):
    """Yield every dict nested in the reply that looks like a UI element."""
    if isinstance(obj, dict):
        if "text" in obj or "menu_index" in obj:
            yield obj
        for value in obj.values():
            yield from iter_elements(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_elements(item)


def poll_observe(client, predicate: Callable[[dict], bool]) -> tuple[dict, bool]:
    """Poll observe until the predicate holds on a stable reply, or time out."""
    deadline = time.monotonic() + POLL_SECONDS
    reply: dict = {}
    while True:
        try:
            reply = client.observe()
            if reply.get("stable") and predicate(reply):
                return reply, True
        except Exception as exc:  # transient bridge errors: keep polling
            reply = {"_poll_error": f"{type(exc).__name__}: {exc}"}
        if time.monotonic() >= deadline:
            return reply, False
        time.sleep(POLL_INTERVAL)


def has_text(text: str) -> Callable[[dict], bool]:
    return lambda reply: any(element.get("text") == text for element in iter_elements(reply))


def has_menu_index(reply: dict) -> bool:
    return any(element.get("menu_index") is not None for element in iter_elements(reply))


def has_dialogue(reply: dict) -> bool:
    return bool(reply.get("dialogue"))


def capture_states(client) -> dict[str, dict]:
    """Drive the fixture through its states; return raw-ish replies keyed by state name."""
    states: dict[str, dict] = {}

    def record(name: str, reply: dict, matched: bool, what: str) -> None:
        if not matched:
            reply = dict(reply)
            reply["_capture_error"] = f"state '{name}' never reached: {what}"
        states[name] = reply

    reply, ok = poll_observe(client, has_dialogue)
    record("say", reply, ok, "no stable dialogue")

    client.advance()
    reply, ok = poll_observe(client, has_menu_index)
    record("menu", reply, ok, "no menu_index element")

    client.select_choice(text="Pick A")
    reply, ok = poll_observe(client, has_text("Enabled"))
    record("hub", reply, ok, "no 'Enabled' element")

    client.eval_expr("renpy.show_screen('rf_modal') or renpy.restart_interaction()")
    reply, ok = poll_observe(client, has_text("Close"))
    record("hub_modal", reply, ok, "no 'Close' element")
    return states


def capture_version(version: str) -> dict[str, dict]:
    sdk = get_or_install_sdk(version)
    with tempfile.TemporaryDirectory() as tmp:
        game_project = Path(tmp) / "fixture"
        (game_project / "game").mkdir(parents=True)
        (game_project / "game" / "script.rpy").write_text(FIXTURE_SCRIPT, encoding="utf-8")
        with launch_with_bridge(sdk, RenpyProject(game_project), startup_timeout=90, editor=False) as session:
            return capture_states(session.client)


def write_states(out_dir: Path, version: str, states: dict[str, dict]) -> list[Path]:
    version_dir = out_dir / version
    version_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, reply in states.items():
        path = version_dir / f"{name}.json"
        path.write_text(json.dumps(normalize(reply), indent=2, sort_keys=True, default=str), encoding="utf-8")
        written.append(path)
    return written


def compare(out_dir: Path, check_dir: Path, paths: list[Path]) -> bool:
    any_diff = False
    for path in paths:
        relative = path.relative_to(out_dir)
        other = check_dir / relative
        new_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if not other.is_file():
            any_diff = True
            print(f"MISSING in {check_dir}: {relative}")
            continue
        old_lines = other.read_text(encoding="utf-8").splitlines(keepends=True)
        if old_lines == new_lines:
            continue
        any_diff = True
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=str(other), tofile=str(path),
        )
        sys.stdout.writelines(diff)
        print()
    return any_diff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path, help="directory to write DIR/<version>/<state>.json")
    parser.add_argument("--versions", default=",".join(DEFAULT_VERSIONS),
                        help="comma-separated Ren'Py versions (default: 8.5.3,8.0.3)")
    parser.add_argument("--check", type=Path, default=None,
                        help="directory to compare the captured files against")
    args = parser.parse_args(argv)

    versions = [v.strip() for v in args.versions.split(",") if v.strip()]
    written: list[Path] = []
    for version in versions:
        print(f"capturing Ren'Py {version}...", flush=True)
        states = capture_version(version)
        for path in write_states(args.out, version, states):
            written.append(path)
            print(f"  wrote {path}")

    if args.check is None:
        return 0
    if compare(args.out, args.check, written):
        print("DIFFERENT: captured observe output differs from the compare directory")
        return 1
    print("MATCH: all captured files equal the compare directory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
