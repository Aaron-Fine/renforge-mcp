"""The injected bridge reads no builtin or import a game's store can replace."""

import dis
import textwrap
import types

from renforge.bridge.guard import GUARDED_BUILTINS, guard_block, guard_rpy
from renforge.bridge.launcher import _EDITOR_RESOURCE, _editor_payload, bridge_payload


def _blocks(text):
    """Each ``init python:`` block's body, dedented, with its first line number."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("init") and line.rstrip().endswith("python:"):
            end = index + 1
            while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "#")):
                end += 1
            body = lines[index + 1:end]
            yield index + 2, "\n".join(line[4:] if line.startswith("    ") else line for line in body)


def _local_names(code):
    names = set(code.co_varnames) | set(code.co_cellvars)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _local_names(const)
    return names


def _global_reads(code):
    names = set()
    for instruction in dis.get_instructions(code):
        if instruction.opname in ("LOAD_GLOBAL", "LOAD_NAME"):
            names.add(instruction.argval)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _global_reads(const)
    return names


def test_injected_scripts_read_no_name_a_game_can_shadow():
    for payload, private_imports in ((bridge_payload(), True), (_editor_payload(), False)):
        for _line, body in _blocks(payload.decode("utf-8")):
            code = compile(body, "block", "exec")
            reads = _global_reads(code)
            assert not reads & GUARDED_BUILTINS
            # Reads of a local named like a builtin would be rewritten to the builtin.
            assert not _local_names(code) & GUARDED_BUILTINS
            if private_imports:
                for name in ("json", "os", "sys", "types", "time"):
                    assert name not in reads


def test_renpy_own_store_names_are_left_to_the_store():
    """Ren'Py's minstore rebinds these; its eval returns revertable lists the bridge serialises."""
    for name in ("list", "dict", "set", "object", "range", "sorted", "eval", "str", "open", "print", "input"):
        assert name not in GUARDED_BUILTINS
    assert "_rfb_eval" not in bridge_payload().decode("utf-8")


def test_guarding_keeps_line_numbers():
    for path in (_EDITOR_RESOURCE, _EDITOR_RESOURCE.with_name("bridge.rpy")):
        text = path.read_text(encoding="utf-8")
        guarded = guard_rpy(text).splitlines()
        assert len(guarded) == len(text.splitlines())
        assert [line for line in guarded if line.lstrip().startswith("def ")] == [
            line for line in text.splitlines() if line.lstrip().startswith("def ")]


def test_only_reads_are_rewritten():
    block = (
        "init python:\n"
        "    # header\n"
        "    import json\n"
        "    def size(node, key=None):\n"
        "        return len(node.id), json.dumps(dict(id=key)), isinstance(node, list)\n"
    )
    guarded = guard_block(block)
    assert "import json as _rfb_json" in guarded
    assert "_rfb_len(node.id)" in guarded  # attribute names keep their spelling
    assert "dict(id=key)" in guarded  # keyword names and Ren'Py's own store types too
    assert "_rfb_isinstance(node, list)" in guarded
    assert guarded.splitlines()[1].endswith("# header")

    store = {"len": lambda *_: "game", "json": "game data", "isinstance": None}
    exec(compile(textwrap.dedent(guarded.split("\n", 1)[1]), "block", "exec"), store)
    assert store["size"](types.SimpleNamespace(id=[1, 2]), key=3) == (2, '{"id": 3}', False)
    assert store["json"] == "game data" and store["len"]() == "game"
