"""Unit tests for style-backed say.who / namebox gui.name_xpos/ypos."""

from __future__ import annotations

import pytest

from renforge.editor.source import (
    SAY_WHO_STYLE_POSITION_MODE,
    EditorSourceError,
    analyze_say_what_style_position,
    apply_say_what_style_position_patch,
)


def _analyze(source: str):
    return analyze_say_what_style_position(
        source,
        xpos_var="gui.name_xpos",
        ypos_var="gui.name_ypos",
        position_mode=SAY_WHO_STYLE_POSITION_MODE,
    )


def test_analyze_say_who_style_position_unlocks_pure_gui_scale() -> None:
    parsed = _analyze(
        "define gui.name_xpos = gui.scale(240)\n"
        "define gui.name_ypos = gui.scale(0)\n"
    )
    assert parsed.position_mode == SAY_WHO_STYLE_POSITION_MODE
    assert parsed.position_lock_code is None
    assert parsed.xpos == 240
    assert parsed.ypos == 0


def test_apply_say_who_style_position_patch_rewrites_only_name_vars() -> None:
    source = (
        "define gui.name_xpos = gui.scale(240)\n"
        "define gui.name_ypos = gui.scale(0)\n"
        "define gui.dialogue_xpos = gui.scale(268)\n"
        "define gui.nvl_thought_xpos = gui.scale(240)\n"
    )
    parsed = _analyze(source)
    patched = apply_say_what_style_position_patch(
        source.encode("utf-8"),
        parsed,
        x=260,
        y=30,
    ).decode("utf-8")
    assert "define gui.name_xpos = gui.scale(260)" in patched
    assert "define gui.name_ypos = gui.scale(30)" in patched
    assert "define gui.dialogue_xpos = gui.scale(268)" in patched
    assert "define gui.nvl_thought_xpos = gui.scale(240)" in patched


def test_analyze_say_who_style_position_rejects_name_variant() -> None:
    parsed = _analyze(
        "define gui.name_xpos = gui.scale(240)\n"
        "define gui.name_ypos = gui.scale(0)\n"
        "\n"
        "init python:\n"
        "    @gui.variant\n"
        "    def small():\n"
        "        gui.name_xpos = gui.scale(90)\n"
    )
    assert parsed.position_lock_code == "STYLE_POSITION_VARIANT_UNSUPPORTED"
    assert parsed.position_mode is None


def test_apply_say_who_style_position_patch_refuses_locked_statement() -> None:
    parsed = _analyze("define gui.name_ypos = gui.scale(0)\n")
    with pytest.raises(EditorSourceError) as exc_info:
        apply_say_what_style_position_patch(
            b"define gui.name_ypos = gui.scale(0)\n",
            parsed,
            x=260,
            y=30,
        )
    assert exc_info.value.code == "STYLE_POSITION_SOURCE_UNRESOLVED"
