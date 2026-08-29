from pathlib import Path

from renforge.autopilot import _menu_choices, _story_labels

_DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo_game"


def test_story_labels_excludes_internal_labels() -> None:
    labels = _story_labels(_DEMO)
    assert {"start", "village_gate", "crossroads", "ending_light", "main_menu"} <= labels
    assert not any(name.startswith("_") for name in labels)


class _ChoiceClient:
    def __init__(self, choices: list[dict], elements: list[dict] | None = None) -> None:
        self.choices = choices
        self.elements = (
            elements
            if elements is not None
            else [
                {
                    **choice,
                    "action": "Return",
                    "enabled": True,
                    "clickable": True,
                }
                for choice in choices
            ]
        )

    def list_choices(self) -> list[dict]:
        return self.choices

    def list_ui_elements(self) -> list[dict]:
        return self.elements


def test_menu_choices_prefers_standard_choice_screen() -> None:
    standard = {"text": "Take the ridge", "screen": "choice"}
    choices = _menu_choices(
        _ChoiceClient(
            [
                {"text": "Custom A", "screen": "story_choices"},
                {"text": "Custom B", "screen": "story_choices"},
                standard,
            ]
        )
    )

    assert choices == [standard]


def test_menu_choices_accepts_multi_control_custom_screen() -> None:
    custom = [
        {"text": "Take the lantern", "screen": "village_gate_choices"},
        {"text": "Stay home", "screen": "village_gate_choices"},
        {"text": "Locked demo", "screen": "village_gate_choices"},
        {"text": "Disabled demo", "screen": "village_gate_choices"},
    ]

    choices = _menu_choices(
        _ChoiceClient(
            [
                {"text": "Save", "screen": "quick_menu"},
                *custom,
            ],
            elements=[
                {**custom[0], "action": "Return", "enabled": True, "clickable": True},
                {**custom[1], "action": "Return", "enabled": True, "clickable": True},
                {**custom[2], "action": "NullAction", "enabled": True, "clickable": True},
                {**custom[3], "action": "Return", "enabled": False, "clickable": False},
            ],
        )
    )

    assert choices == custom[:2]


def test_menu_choices_ignores_overlays_and_single_story_controls() -> None:
    choices = _menu_choices(
        _ChoiceClient(
            [
                {"text": "Save", "screen": "quick_menu"},
                {"text": "Preferences", "screen": "quick_menu"},
                {"text": "RF", "screen": "_renforge_editor_launcher"},
                {"text": "Touch shrine", "screen": "shrine_controls"},
            ]
        )
    )

    assert choices == []


def test_menu_choices_does_not_inventory_ui_without_a_candidate_group() -> None:
    class _OverlayOnlyClient:
        def list_choices(self) -> list[dict]:
            return [
                {"text": "Save", "screen": "quick_menu"},
                {"text": "Preferences", "screen": "quick_menu"},
            ]

        def list_ui_elements(self) -> list[dict]:
            raise AssertionError("UI inventory should not be requested")

    assert _menu_choices(_OverlayOnlyClient()) == []
