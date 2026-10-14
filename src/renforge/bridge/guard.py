"""Keep the injected bridge's own names out of reach of the game's store.

``init python`` code runs with the game's store as its globals, so a bare
``id(node)`` in the bridge finds a game's ``define id = Character("Id")``
before the builtin (Eternum), and a game's ``default types = [...]`` replaces
the ``types`` module the bridge imported. Rewriting the bridge source at
injection time binds each builtin and each top-level import to a private
``_rfb_`` name, which no game defines, while the source stays plain Python.

Ren'Py itself puts revertable ``list``, ``dict``, ``set``, ``object``,
``range``, and ``sorted`` in the store; the bridge keeps using those as it
always has (``builtins.list`` where it needs the plain type).
"""

from __future__ import annotations

import builtins
import io
import re
import tokenize

PREFIX = "_rfb_"

_RENPY_STORE_NAMES = frozenset({"list", "dict", "set", "object", "range", "sorted"})
_CONSTANTS = frozenset({"True", "False", "None", "Ellipsis", "NotImplemented"})
GUARDED_BUILTINS = frozenset(
    name for name in dir(builtins)
    if not name.startswith("_") and name not in _CONSTANTS and name not in _RENPY_STORE_NAMES
)
_TOP_IMPORT = re.compile(r"^    import (\w+)\s*(?:#.*)?$")
_HEADER = re.compile(r"^init(?: -?\d+)? python:\s*$")
_SIMPLE_STATEMENT = re.compile(r"^    (?:import |from |[A-Za-z_][\w.]* = )")


def guard_block(source: str) -> str:
    """Rewrite one ``init python:`` block so builtins and its imports are private.

    Line numbers are unchanged: the alias bindings replace the first comment
    line of the block. A name is rewritten only where it is read; attribute
    names, keyword arguments, and definitions keep their spelling.
    """
    lines = source.splitlines(keepends=True)
    if not lines or not _HEADER.match(lines[0]):
        raise ValueError("expected a block that starts with 'init python:'")
    modules = {m.group(1) for line in lines[1:] if (m := _TOP_IMPORT.match(line))}
    guarded = GUARDED_BUILTINS | modules

    tokens = [t for t in tokenize.generate_tokens(io.StringIO(source).readline)
              if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT,
                                tokenize.INDENT, tokenize.DEDENT)]
    edits: list[tuple[int, int, int, str]] = []
    used: set[str] = set()
    for index, token in enumerate(tokens):
        if token.type != tokenize.NAME or token.string not in guarded:
            continue
        before = tokens[index - 1].string if index else ""
        after = tokens[index + 1].string if index + 1 < len(tokens) else ""
        row, col = token.start
        if before == "import" and token.string in modules and after != "as":
            # `import json` binds the private name instead.
            edits.append((row, col, token.end[1], f"{token.string} as {PREFIX}{token.string}"))
            continue
        if before in (".", "def", "class", "from", "import", "as") or after == "=":
            continue
        used.add(token.string)
        edits.append((row, col, token.end[1], PREFIX + token.string))

    for row, start, end, text in sorted(edits, reverse=True):
        line = lines[row - 1]
        lines[row - 1] = line[:start] + text + line[end:]

    builtin_names = sorted(used - modules)
    if builtin_names:
        binding = "import builtins as _rfb_b; " + "; ".join(
            f"{PREFIX}{name} = _rfb_b.{name}" for name in builtin_names)
        for index in range(1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("#"):
                lines[index] = f"    {binding}  {stripped}\n"
                break
            if _SIMPLE_STATEMENT.match(lines[index]):
                lines[index] = f"    {binding}; {lines[index].lstrip()}"
                break
            if stripped:
                raise ValueError("the block must open with a comment or a simple statement")
    return "".join(lines)


def guard_rpy(text: str) -> str:
    """Guard every store ``init python:`` block of a script; nothing else changes.

    ``init python in <store>`` blocks run in their own namespace, which game
    variables do not reach, so they are left alone, as are screens.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    index = 0
    while index < len(lines):
        if not _HEADER.match(lines[index]):
            out.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t", "#")):
            end += 1
        out.append(guard_block("".join(lines[index:end])))
        index = end
    return "".join(out)
