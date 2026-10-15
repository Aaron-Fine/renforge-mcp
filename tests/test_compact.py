from renforge.compact import compact_control, compact_observation


def _control(**overrides):
    control = {
        "id": "hub/go", "role": "button", "text": "Go", "screen": "hub", "action": "Jump('x')",
        "image": None, "hovered": None, "selected": False, "adjustment": None, "draggable": None,
        "operations": ["click"], "enabled": True, "clickable": True, "covered": False,
        "bounds": {"x": 10, "y": 20, "width": 40, "height": 16}, "visible": True,
        "bounds_reason": None, "click_point": {"x": 30, "y": 28}, "semantic": False,
        "occurrences": [{"bounds": {"x": 10, "y": 20, "width": 40, "height": 16}, "render_path": [1]}],
        "layer": "screens", "synthetic": False, "source": "game/screens.rpy:9",
    }
    control.update(overrides)
    return control


def test_compact_control_keeps_what_an_agent_acts_on():
    assert compact_control(_control()) == {
        "id": "hub/go", "role": "button", "text": "Go", "action": "Jump('x')",
        "operations": ["click"], "bounds": {"x": 10, "y": 20, "width": 40, "height": 16},
        "source": "game/screens.rpy:9",
    }


def test_compact_control_reports_unusual_states():
    out = compact_control(_control(enabled=False, clickable=False, covered=True, selected=True,
                                   click_point={"x": 12, "y": 22}))
    assert out["enabled"] is False and out["clickable"] is False
    assert out["covered"] is True and out["selected"] is True
    assert out["click_point"] == {"x": 12, "y": 22}


def test_compact_observation_reduces_chrome_and_repeats():
    snapshot = {
        "label": "start", "dialogue": None, "frame_id": "f", "frame_hash": "f",
        "frame": {"frame_id": "f", "session_id": "s", "width": 8, "height": 4},
        "screenshot": {"width": 8, "height": 4},
        "omitted": {"focus_truncated": False, "unclassified": False},
        "controls": [_control()],
        "chrome": [_control(id="quick_menu/Skip", text="Skip", enabled=False)],
        "readout": [{"screen": "hud", "text": "Day 1", "bounds": {"x": 0}}],
        "screens": [{"name": "hud", "layer": "screens", "modal": False, "overlay": False}],
    }
    out = compact_observation(snapshot)
    assert "dialogue" not in out and "frame_hash" not in out and "screenshot" not in out
    assert "frame_id" not in out  # frame carries it
    assert "omitted" not in out
    assert out["frame"] == {"frame_id": "f", "width": 8, "height": 4}
    assert out["chrome"] == [{"id": "quick_menu/Skip", "text": "Skip", "enabled": False}]
    assert out["readout"] == [{"screen": "hud", "text": "Day 1"}]
    assert out["screens"] == [{"name": "hud", "layer": "screens"}]
    assert snapshot["dialogue"] is None  # the original is not mutated
