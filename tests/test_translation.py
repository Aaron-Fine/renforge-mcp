from pathlib import Path
from types import SimpleNamespace

import pytest

from renforge import translation
from renforge.project import RenpyProject
from renforge.translation import _COUNT_RE, list_languages, list_translation_strings


class _FakeSdk:
    def launch_command(self, root, *args):
        return ["renpy", *args]


def _project(tmp_path: Path) -> RenpyProject:
    (tmp_path / "game").mkdir()
    return RenpyProject(tmp_path)


def test_list_languages_reads_tl_dir(tmp_path: Path) -> None:
    tl = tmp_path / "proj" / "game" / "tl"
    (tl / "french").mkdir(parents=True)
    (tl / "spanish").mkdir()
    (tl / "None").mkdir()  # reserved, must be excluded

    assert list_languages(tmp_path / "proj") == ["french", "spanish"]


def test_list_languages_without_tl_dir(tmp_path: Path) -> None:
    (tmp_path / "proj" / "game").mkdir(parents=True)
    assert list_languages(tmp_path / "proj") == []


def test_count_regex_parses_translate_count_output() -> None:
    line = "french: 12 missing dialogue translations, 3 missing string translations."
    m = _COUNT_RE.search(line)
    assert m and int(m.group("dialogue")) == 12 and int(m.group("strings")) == 3


def test_generate_translations_reports_the_tl_directory_on_success(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        translation,
        "run_command",
        lambda command, timeout, env=None: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = translation.generate_translations(_FakeSdk(), _project(tmp_path), "french")

    assert result["ok"] is True
    assert result["files_touched"] == ["game/tl/french"]


def test_generate_translations_failure_omits_the_receipt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        translation,
        "run_command",
        lambda command, timeout, env=None: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    result = translation.generate_translations(_FakeSdk(), _project(tmp_path), "french")

    assert result["ok"] is False
    assert "files_touched" not in result


def test_export_dialogue_failure_omits_the_receipt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        translation,
        "run_command",
        lambda command, timeout, env=None: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    result = translation.export_dialogue(_FakeSdk(), _project(tmp_path))

    assert result["ok"] is False
    assert "files_touched" not in result


def test_export_dialogue_reports_the_project_root_output(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        translation,
        "run_command",
        lambda command, timeout, env=None: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = translation.export_dialogue(_FakeSdk(), _project(tmp_path))

    assert result["ok"] is True
    assert result["files_touched"] == ["dialogue.txt"]


def test_list_translation_strings_keeps_blocks_separate(tmp_path: Path) -> None:
    tl = tmp_path / "proj" / "game" / "tl" / "french"
    tl.mkdir(parents=True)
    (tl / "script.rpy").write_text(
        '''
translate french start_abcd:
    # "Hello."
    "Bonjour."

translate french next_efgh:
    # "Bye."
    "Au revoir."
''',
        encoding="utf-8",
    )

    rows = list_translation_strings(tmp_path / "proj", "french")

    assert [(row["id"], row["src"], row["tr"]) for row in rows] == [
        ("start_abcd", "Hello.", "Bonjour."),
        ("next_efgh", "Bye.", "Au revoir."),
    ]


def test_list_translation_strings_handles_escaped_quotes_and_skips_non_dialogue(tmp_path: Path) -> None:
    tl = tmp_path / "proj" / "game" / "tl" / "french"
    tl.mkdir(parents=True)
    (tl / "script.rpy").write_text(
        r'''
translate french start_abcd:
    # "Say \"hello\"."
    voice "voice/line.ogg"
    "Dis \"bonjour\"."
''',
        encoding="utf-8",
    )

    rows = list_translation_strings(tmp_path / "proj", "french")

    assert rows[0]["src"] == 'Say "hello".'
    assert rows[0]["tr"] == 'Dis "bonjour".'


def test_list_translation_strings_generates_unique_string_ids_across_blocks(tmp_path: Path) -> None:
    tl = tmp_path / "proj" / "game" / "tl" / "french"
    tl.mkdir(parents=True)
    (tl / "common.rpy").write_text(
        '''
translate french strings:
    old "Start"
    new "Démarrer"

translate french strings:
    old "Quit"
    new "Quitter"
''',
        encoding="utf-8",
    )

    rows = list_translation_strings(tmp_path / "proj", "french")

    assert [(row["id"], row["src"], row["tr"]) for row in rows] == [
        ("string_1", "Start", "Démarrer"),
        ("string_2", "Quit", "Quitter"),
    ]


@pytest.mark.parametrize("language", ["../../elsewhere", "fr/../..", "", "french spanish", None, 3])
@pytest.mark.parametrize(
    "call",
    [translation.generate_translations, translation.translation_stats, translation.export_dialogue],
    ids=["generate", "stats", "export"],
)
def test_renpy_commands_refuse_a_language_that_is_not_a_name(
    tmp_path: Path, monkeypatch, call, language
) -> None:
    """Ren'Py joins the language into game/tl/<language>/ unchecked, so '..' would write outside it."""

    def no_run(*args, **kwargs):
        raise AssertionError("Ren'Py must not run for an invalid language")

    monkeypatch.setattr(translation, "run_command", no_run)

    result = call(_FakeSdk(), _project(tmp_path), language)

    assert result["ok"] is False
    assert result["code"] == "LANGUAGE_INVALID"
    assert "files_touched" not in result


def test_export_dialogue_accepts_the_none_language(tmp_path: Path, monkeypatch) -> None:
    seen = {}

    def fake_run(command, timeout, env=None):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(translation, "run_command", fake_run)

    assert translation.export_dialogue(_FakeSdk(), _project(tmp_path))["ok"] is True
    assert "None" in seen["command"]


def test_list_translation_strings_refuses_a_language_that_is_not_a_name(tmp_path: Path) -> None:
    (tmp_path / "game" / "tl" / "french").mkdir(parents=True)
    (tmp_path / "game" / "tl" / "french" / "x.rpy").write_text(
        'translate french strings:\n    old "a"\n    new "b"\n', encoding="utf-8"
    )
    assert list_translation_strings(tmp_path, "french/.") == []
    assert translation.invalid_language("french") is None
    assert translation.invalid_language("../x")["code"] == "LANGUAGE_INVALID"
