"""Autopilot: replay a Ren'Py story to explore menus and report outcomes.

Each run launches the game fresh and replays a fixed prefix of earlier menu
selections. At the next new menu, Autopilot selects the first choice and queues
the remaining choices as prefixes for later runs. It collects label and dialogue
events after every interaction, records bridge exceptions as crashes, and stops
a run when a story label repeats or the step limit is reached.

The bridge derives the current story choices from the active menu screen's
``items`` scope and maps actionable item captions to visible controls. This is
the data Ren'Py supplies to standard, custom-named, NVL, and one-item menu
screens. Other ``call_screen`` interactions that do not expose menu ``items``
are not currently explored automatically.

Ren'Py menu screens: https://www.renpy.org/doc/html/menus.html#menu-arguments
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .bridge.launcher import launch_with_bridge
from .project import RenpyProject
from .scanner import scan_project
from .sdk import RenpySdk


def _story_labels(project_path: str | Path) -> set[str]:
    # Labels authored in the project, excluding Ren'Py-internal (underscore) ones.
    index = scan_project(str(project_path))
    return {label["name"] for label in index.get("labels", []) if not label["name"].startswith("_")}


def _menu_choices(client) -> list[dict]:
    """Return the active story-menu controls in bridge display order."""
    # TODO: Add an opt-in game-specific adapter hook for custom call_screen
    # interactions that do not expose Ren'Py menu items. Do not infer narrative
    # intent from control count or screen names.
    return [
        choice
        for choice in client.list_menu_choices()
        if isinstance(choice, dict) and choice.get("text")
    ]


def autopilot(
    sdk: RenpySdk,
    project: RenpyProject,
    *,
    max_runs: int = 16,
    max_steps: int = 60,
    settle: float = 0.4,
    startup_timeout: float = 90.0,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """Explore the game and return a coverage/crash report.

    After each run a partial report is written to
    ``<project>/.renforge/autopilot.json`` and passed to ``progress_callback``
    (if given), so long explorations can be followed incrementally.
    """
    total_labels = _story_labels(project.root)

    reached: set[str] = set()
    dialogue: set[str] = set()
    crashes: list[dict] = []
    choices_explored = 0

    frontier: deque[list[int]] = deque([[]])
    seen_prefixes: set[tuple[int, ...]] = set()
    runs = 0

    progress_path = project.cache_dir / "autopilot.json"

    def _report(done: bool) -> dict[str, Any]:
        covered = sorted(total_labels & reached)
        return {
            "ok": True,
            "done": done,
            "runs": runs,
            "runs_pending": len(frontier),
            "labels_total": len(total_labels),
            "labels_reached": sorted(reached),
            "labels_covered": covered,
            "labels_unreached": sorted(total_labels - reached),
            "coverage": round(len(covered) / len(total_labels), 3) if total_labels else 1.0,
            "dialogue_lines": len(dialogue),
            "choices_explored": choices_explored,
            "crashes": crashes,
        }

    def _emit(done: bool) -> None:
        report = _report(done)
        try:
            progress_path.write_text(json.dumps(report), encoding="utf-8")
        except OSError:
            pass
        if progress_callback is not None:
            try:
                progress_callback(report)
            except Exception:
                pass

    def _drain(client, cursor: int, run_labels: list[str]) -> int:
        events = client.poll_events(since=cursor)
        for event in events["events"]:
            kind = event.get("type")
            if kind == "label":
                name = event.get("label")
                if name and not name.startswith("_"):
                    reached.add(name)
                    run_labels.append(name)
            elif kind == "say":
                dialogue.add(event.get("what"))
            elif kind == "exception":
                crashes.append({"short": event.get("short"), "full": event.get("full")})
        return events["cursor"]

    while frontier and runs < max_runs:
        prefix = frontier.popleft()
        if tuple(prefix) in seen_prefixes:
            continue
        seen_prefixes.add(tuple(prefix))
        runs += 1

        session = launch_with_bridge(sdk, project, startup_timeout=startup_timeout)
        try:
            cursor = 0
            seq: list[int] = list(prefix)
            seq_pos = 0
            run_labels: list[str] = []

            for _step in range(max_steps):
                cursor = _drain(session.client, cursor, run_labels)

                # Loop guard: a repeated story label means the game cycled back
                # (e.g. returned to the main menu); stop this run so we don't
                # keep re-answering the same menu forever.
                if len(run_labels) != len(set(run_labels)):
                    break

                choices = _menu_choices(session.client)
                if choices:
                    # Let the menu's show transition finish so the choice buttons
                    # are at stable positions before we click one.
                    time.sleep(settle)
                    choices = _menu_choices(session.client) or choices
                    if seq_pos < len(seq):
                        idx = seq[seq_pos]
                    else:
                        # New branch point: take choice 0, queue the alternatives.
                        for alt in range(1, len(choices)):
                            frontier.append(seq + [alt])
                        idx = 0
                        seq.append(0)
                    seq_pos += 1
                    idx = min(idx, len(choices) - 1)
                    # Select by visible text (the reliable focus-resolution path).
                    session.client.select_choice(text=choices[idx]["text"])
                    choices_explored += 1
                    time.sleep(settle)
                    continue

                session.client.advance()
                time.sleep(settle)

            _drain(session.client, cursor, run_labels)
        finally:
            session.close()

        _emit(done=not frontier or runs >= max_runs)

    return _report(done=True)


__all__ = ["autopilot"]
