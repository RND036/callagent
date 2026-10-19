import shutil

import pytest
import yaml

import languages
from gemini_session.persona import build_system_instruction, language_switched_note
from languages import LANG_DIR, LanguageFileError, load_languages


def _keys(data):
    """Top-level keys plus those of the always-present blocks (time / dates / branches may be null or empty)."""
    for k, v in data.items():
        yield k
        if k in ("lines", "phrases", "numbers"):
            yield from (f"{k}.{sub}" for sub in v)


def test_files_load_in_order_with_the_same_keys():
    langs = languages.languages()
    assert list(langs) == ["si", "ta", "en"]
    keysets = [set(_keys(yaml.safe_load((LANG_DIR / f"{c}.yaml").read_text(encoding="utf-8")))) for c in langs]
    assert keysets[0] == keysets[1] == keysets[2]
    assert [l.menu_digit for l in langs.values()] == ["1", "2", "3"]


@pytest.fixture
def copy(tmp_path):
    for f in LANG_DIR.glob("*.yaml"):
        shutil.copy(f, tmp_path / f.name)
    return tmp_path


def _edit(directory, code, fn):
    path = directory / f"{code}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_a_missing_key_names_the_file_and_key(copy):
    _edit(copy, "ta", lambda d: d["lines"].pop("anything_else"))
    with pytest.raises(LanguageFileError, match=r"ta\.yaml: missing key 'lines\.anything_else'"):
        load_languages(copy)


def test_a_misspelt_key_is_refused(copy):
    _edit(copy, "en", lambda d: d.update(greating=d.pop("greeting")))
    with pytest.raises(LanguageFileError, match=r"en\.yaml: (missing key 'greeting'|unknown key 'greating')"):
        load_languages(copy)


def test_an_unknown_placeholder_is_refused(copy):
    _edit(copy, "si", lambda d: d.update(greeting="ආයුබෝවන් {salon}"))
    with pytest.raises(LanguageFileError, match=r"si\.yaml: greeting: unknown placeholder \{salon\}"):
        load_languages(copy)


def test_a_missing_required_placeholder_is_refused(copy):
    _edit(copy, "en", lambda d: d["lines"].update(change_language="Press the hash key."))
    with pytest.raises(LanguageFileError, match=r"en\.yaml: lines\.change_language: must contain \{key\}"):
        load_languages(copy)


def test_the_code_must_match_the_file_name(copy):
    _edit(copy, "ta", lambda d: d.update(code="xx"))
    with pytest.raises(LanguageFileError, match=r"ta\.yaml: code is 'xx'"):
        load_languages(copy)


def test_two_languages_cannot_share_a_menu_digit(copy):
    _edit(copy, "en", lambda d: d.update(menu_digit="1"))
    with pytest.raises(LanguageFileError, match=r"menu_digit '1' is already used by en\.yaml"):
        load_languages(copy)


def test_number_words_need_the_exact_count(copy):
    _edit(copy, "si", lambda d: d["numbers"].update(words=d["numbers"]["words"][:50]))
    with pytest.raises(LanguageFileError, match=r"si\.yaml: numbers\.words"):
        load_languages(copy)


def test_a_call_prompt_quotes_only_its_own_language():
    si = build_system_instruction(language="si")
    assert "අනිවාර්යයෙන්ම" in si and "உங்களுக்கு உதவுவதில்" not in si and "I'd love to help" not in si
    assert "வேறு ஏதாவது" not in si and "[தொகை] ரூபாய்" not in si
    en = build_system_instruction(language="en")
    assert "I'd love to help" in en and "අනිවාර්යයෙන්ම" not in en and "(LKR / rupees)" in en


def test_the_change_language_rule_lists_every_language():
    text = build_system_instruction(language="ta", language_menu_key="#")
    for line in ("භාෂාව වෙනස් කරන්න", "மொழியை மாற்ற", "To change the language"):
        assert line in text


def test_phrases_match_across_languages():
    assert languages.pattern("goodbye").search("ස්තූතියි, සුබ දවසක්") and languages.pattern("goodbye").search("Bye bye")
    assert languages.pattern("anything_else").search("வேறு ஏதாவது தெரிந்துகொள்ள வேண்டுமா?")


def test_phone_numbers_are_said_per_language():
    langs = languages.languages()
    assert langs["si"].numbers.say_phone("0771234567") == "බිංදුව හැත්තෑ හත, එකසිය විසි තුන, හතළිස් පහ, හැට හත"
    assert langs["en"].numbers.say_phone("0771234567") == "zero seven seven, one two three, four five six seven"
