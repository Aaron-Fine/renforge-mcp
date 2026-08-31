from pathlib import Path

from renforge.autopilot import _menu_choices, _story_labels

_DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo_game"


def test_story_labels_excludes_internal_labels() -> None:
    labels = _story_labels(_DEMO)
    assert {"start", "village_gate", "crossroads", "ending_light", "main_menu"} <= labels
    assert not any(name.startswith("_") for name in labels)


class _ChoiceClient:
    def __init__(self, choices: list[dict]) -> None:
        self.choices = choices

    def list_choices(self) -> list[dict]:
        return self.choices


def test_menu_choices_accepts_one_proven_menu_item() -> None:
    menu_item = {
        "text": "Continue",
        "screen": "kinetic_continue",
        "menu_item": True,
    }

    assert _menu_choices(_ChoiceClient([menu_item])) == [menu_item]


def test_menu_choices_returns_only_proven_menu_items() -> None:
    menu_items = [
        {"text": "Take the ridge", "screen": "story_menu", "menu_item": True},
        {"text": "Stay home", "screen": "story_menu", "menu_item": True},
    ]
    controls = [
        {"text": "Save", "screen": "quick_menu"},
        *menu_items,
        {"text": "Inventory", "screen": "hud"},
        {"text": "Map", "screen": "hud"},
    ]

    assert _menu_choices(_ChoiceClient(controls)) == menu_items


def test_menu_choices_does_not_infer_custom_choices_from_control_count() -> None:
    controls = [
        {"text": "Custom A", "screen": "custom_screen"},
        {"text": "Custom B", "screen": "custom_screen"},
    ]

    assert _menu_choices(_ChoiceClient(controls)) == []
