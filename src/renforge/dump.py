"""Authoritative project definitions via Ren'Py's native ``--json-dump``.

Running a custom Ren'Py CLI command that executes the game's init code is not
viable headlessly: under a dummy/no-audio driver, common init blocks (e.g.
``00mixers.rpy`` at ``init 1600``) reference ``renpy.music`` / ``renpy.list_files``
and similar attributes that are only wired up during a full display+audio init,
which raises ``AttributeError`` before our command runs.

Ren'Py's built-in ``compile --json-dump`` instead introspects the *parsed*
script without executing a full display+audio init, so it works in any
headless environment. It yields exact ``file:line`` locations for labels,
defines, screens, transforms and callables. The narrative *flow* graph
(jumps / menus / says) comes from the fast regex scanner (see
:mod:`renforge.scanner`); exact AST-level flow is a runtime concern handled
later through the in-game bridge, where every ``renpy.*`` module is fully
loaded.

From at least Ren'Py 8.4.1, ``Script.namemap`` is keyed by ``Node``
(``self.namemap[node] = node``; ``Node.__hash__`` / ``__eq__`` use
``.name``) while 8.5.3 ``dump.py`` still filters with
``isinstance(name, str)``, which drops every label. Upstream master
``dump.py`` unwraps ``Node`` keys before that check; the namemap itself
stays Node-keyed. RenForge follows that dump unwrap in the dump subprocess
instead of rewriting installed or cached SDK files: each
``compile --json-dump`` injects a temporary ``.rpe`` adapter via
``RENPY_SEARCHPATH``, the extension form every Ren'Py 8 loads (``.rpe.py``
came only in 8.3). The adapter still normalizes Node-keyed namemaps
even when ``dump.py`` already unwraps; it is a no-op only when keys are
already strings. The pinned default SDK is 8.5.3; compatible 8.4.1+ and
8.5.x installs are supported.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .project import RenpyProject
from .save_isolation import renpy_tool_environment, write_renpy_extension
from .sdk import RenpySdk
from .util.subprocess import run_command

_DEFINITION_CATEGORIES = ("label", "define", "screen", "transform", "callable")

JSON_DUMP_ADAPTER_FILENAME = "renforge_json_dump.rpe"

# The autorun.py of the .rpe, executed inside the Ren'Py dump subprocess
# (bundled Python, no renforge import). Keep this self-contained and
# compatible with every Ren'Py 8: on string-keyed namemaps (before 8.4) it
# changes nothing.
JSON_DUMP_ADAPTER_SOURCE = '''# renforge json-dump adapter — unwrap Node-keyed namemap without editing SDK files
import renpy.dump as _renforge_dump_mod

if not getattr(_renforge_dump_mod.dump, "_renforge_json_dump_adapter", False):
    _renforge_original_dump = _renforge_dump_mod.dump

    def _renforge_unwrap_namemap_key(name):
        if isinstance(name, str):
            return name
        return getattr(name, "name", name)

    def _renforge_adapted_dump(error):
        import renpy

        script = getattr(renpy.game, "script", None)
        namemap = getattr(script, "namemap", None) if script is not None else None
        if not namemap:
            return _renforge_original_dump(error)

        unwrapped = {}
        needs_unwrap = False
        for name, node in namemap.items():
            key = _renforge_unwrap_namemap_key(name)
            if key is not name:
                needs_unwrap = True
            if isinstance(key, str):
                unwrapped[key] = node

        if not needs_unwrap:
            return _renforge_original_dump(error)

        original = script.namemap
        script.namemap = unwrapped
        try:
            return _renforge_original_dump(error)
        finally:
            script.namemap = original

    _renforge_adapted_dump._renforge_json_dump_adapter = True
    _renforge_dump_mod.dump = _renforge_adapted_dump
'''


def _json_dump_searchpath_env(adapter_dir: Path, env: dict[str, str]) -> dict[str, str]:
    """``env`` (from renpy_tool_environment) with the dump adapter searched first."""
    return dict(env, RENPY_SEARCHPATH=f"{adapter_dir}::{env['RENPY_SEARCHPATH']}")


def run_native_dump(sdk: RenpySdk, project: RenpyProject, *, timeout: int = 180) -> dict[str, Any]:
    """Return Ren'Py's native JSON dump for ``project``.

    This compiles the project (writing ``.rpyc`` next to sources, as Ren'Py
    normally does) and introspects the parsed script. Raises ``RuntimeError``
    if Ren'Py produced no dump file.

    The dump subprocess receives an isolated ``.rpe`` adapter on
    ``RENPY_SEARCHPATH``. SDK files under ``sdk.root`` are never modified.
    """
    with tempfile.TemporaryDirectory(prefix="renforge-dump-") as work_home:
        # The adapter folder goes on RENPY_SEARCHPATH; the output folder must
        # not, so the two are siblings. Leaving the block removes both, along
        # with the <out>.new partial Ren'Py leaves when it is cut off.
        adapter_dir = Path(work_home) / "adapter"
        out_dir = Path(work_home) / "out"
        adapter_dir.mkdir()
        out_dir.mkdir()
        write_renpy_extension(adapter_dir / JSON_DUMP_ADAPTER_FILENAME, JSON_DUMP_ADAPTER_SOURCE)
        out_path = out_dir / "dump.json"

        command = project.renpy_command(sdk, ("compile", "--json-dump", str(out_path)))
        with renpy_tool_environment() as env:
            result = run_command(
                command,
                timeout=timeout,
                env=_json_dump_searchpath_env(adapter_dir, env),
            )

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError(
                "Ren'Py produced no JSON dump "
                f"(returncode={result.returncode}, timed_out={result.timed_out}).\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return json.loads(out_path.read_text(encoding="utf-8"))


def normalize_definitions(raw_dump: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``raw_dump['location']`` into a sorted list of definitions.

    Each entry: ``{"name", "kind", "file", "line"}`` where ``kind`` is one of
    ``label|define|screen|transform|callable``.
    """
    location = raw_dump.get("location", {}) or {}
    definitions: list[dict[str, Any]] = []

    for kind in _DEFINITION_CATEGORIES:
        entries = location.get(kind, {}) or {}
        for name, where in entries.items():
            file_name = where[0] if isinstance(where, (list, tuple)) and where else None
            line = where[1] if isinstance(where, (list, tuple)) and len(where) > 1 else None
            definitions.append({"name": name, "kind": kind, "file": file_name, "line": line})

    definitions.sort(key=lambda d: (d["kind"], d.get("file") or "", d.get("line") or 0))
    return definitions


__all__ = [
    "JSON_DUMP_ADAPTER_FILENAME",
    "JSON_DUMP_ADAPTER_SOURCE",
    "run_native_dump",
    "normalize_definitions",
]
