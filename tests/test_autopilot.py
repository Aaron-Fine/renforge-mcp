from pathlib import Path

from renforge.autopilot import _menu_choices, _story_labels

_DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo_game"


def test_story_labels_excludes_internal_labels() -> None:
    labels = _story_labels(_DEMO)
    assert {"start", "village_gate", "crossroads", "ending_light", "main_menu"} <= labels
    assert not any(name.startswith("_") for name in labels)


class _ChoiceClient:
    def __init__(self, menu_choices: list[dict]) -> None:
        self.menu_choices = menu_choices

    def list_menu_choices(self) -> list[dict]:
        return self.menu_choices

    def list_choices(self) -> list[dict]:
        raise AssertionError("Autopilot must not depend on the broad list_choices schema")


def test_menu_choices_accepts_one_proven_menu_item() -> None:
    menu_choice = {
        "index": 2,
        "text": "Continue",
        "screen": "kinetic_continue",
    }

    assert _menu_choices(_ChoiceClient([menu_choice])) == [menu_choice]


def test_menu_choices_returns_only_bridge_proven_menu_items() -> None:
    menu_choices = [
        {"index": 1, "text": "Take the ridge", "screen": "story_menu"},
        {"index": 2, "text": "Stay home", "screen": "story_menu"},
    ]

    assert _menu_choices(_ChoiceClient(menu_choices)) == menu_choices


def test_menu_choices_does_not_infer_custom_choices_from_control_count() -> None:
    assert _menu_choices(_ChoiceClient([])) == []


def test_autopilot_launches_are_isolated_whatever_the_server_default(tmp_path, monkeypatch) -> None:
    """renforge_autopilot has no authorize; RENFORGE_ISOLATION=existing must not hand it the player's saves."""
    import renforge.autopilot as autopilot_module
    from renforge.project import RenpyProject

    monkeypatch.setenv("RENFORGE_ISOLATION", "existing")
    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path / "state"))
    seen = {}

    def fake_launch(sdk, project, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after the first launch")

    monkeypatch.setattr(autopilot_module, "launch_with_bridge", fake_launch)
    try:
        autopilot_module.autopilot(object(), RenpyProject(_DEMO), max_runs=1)
    except RuntimeError:
        pass
    assert seen.get("savedir") == "temporary"
    assert seen.get("home") == "temporary"
