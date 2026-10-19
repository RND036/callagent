import pytest

from datetime import date

from tools.spoken import (branch_from_native, classify_reply, local_number, spoken_branch, spoken_date, spoken_number,
                          spoken_time)


@pytest.mark.parametrize("raw, expected", [
    ("0781827743", "0781827743"), ("+94781827743", "0781827743"), ("94781827743", "0781827743"),
    ("0094781827743", "0781827743"), ("781827743", "0781827743"), ("078 182 774", None), ("", None),
])
def test_local_number(raw, expected) -> None:
    assert local_number(raw) == expected


def test_spoken_number_in_each_language() -> None:
    assert spoken_number("0781827743", "si") == "බිංදුව හැත්තෑ අට, එකසිය අසූ දෙක, හැත්තෑ හත, හතළිස් තුන"
    assert spoken_number("0770051000", "si") == "බිංදුව හැත්තෑ හත, බිංදුව බිංදුව පහ, දහය, බිංදුව බිංදුව"
    assert spoken_number("0781827743", "en") == "zero seven eight, one eight two, seven seven four three"
    assert spoken_number("0781827743", "ta").startswith("பூஜ்ஜியம் ஏழு எட்டு")


def test_branch_names() -> None:
    assert spoken_branch("Nugegoda", "si") == "නුගේගොඩ"
    assert spoken_branch("Kandy", "si") == "Kandy"
    assert branch_from_native("නුගේගොඩ") == "Nugegoda" and branch_from_native("நுகேகொட") == "Nugegoda"
    assert branch_from_native("Kandy") is None


@pytest.mark.parametrize("text, expected", [
    ("ඔව්", "yes"), ("හරි හරි", "yes"), ("yes that is correct", "yes"), ("ஆம்", "yes"),
    ("නැහැ, අංකය වැරදියි", "no"), ("no that's wrong", "no"), ("ඔව් ඒත් නම වැරදියි", "no"),
    ("ප්‍රශ්නයක් නෑ", "yes"), ("no problem", "yes"),
    ("ඔක්කොම", "unclear"), ("", "unclear"),
    ("ඔව් නිවැරදි.", "yes"), ("නිවැරදියි", "yes"), ("නිවැරදි නෑ", "no"), ("වැරදියි", "no"), ("ඔව් වැරදි", "no"),
])
def test_classify_reply(text, expected) -> None:
    assert classify_reply(text) == expected


@pytest.mark.parametrize("value, expected", [
    ("09:00", "උදේ නවයට"), ("11:00", "උදේ එකොළහට"), ("12:00", "දවල් දොළහට"), ("13:30", "දවල් එකහමාරට"),
    ("15:00", "හවස තුනට"), ("19:00", "රෑ හතට"), ("10:15", "උදේ දහයයි පහළොවට"),
])
def test_spoken_time_sinhala(value, expected) -> None:
    assert spoken_time(value, "si") == expected
    assert "පෙ.ව" not in expected and "AM" not in expected


def test_spoken_time_other_languages_keep_am_pm() -> None:
    assert spoken_time("11:00", "en") == "11:00 AM" and spoken_time("15:00", "ta") == "3:00 PM"


@pytest.mark.parametrize("value, expected", [
    ("2026-10-15", "අද, ඔක්තෝබර් පහළොස්වෙනිදා"), ("2026-10-16", "හෙට, ඔක්තෝබර් දාසයවෙනිදා"),
    ("2026-10-17", "අනිද්දා, ඔක්තෝබර් දහහත්වෙනිදා"), ("2026-10-18", "මේ ඉරිදා, ඔක්තෝබර් දහඅටවෙනිදා"),
    ("2026-10-19", "ලබන සතියේ සඳුදා, ඔක්තෝබර් දහනවවෙනිදා"), ("2026-10-26", "ඔක්තෝබර් විසිහයවෙනිදා, සඳුදා"),
])
def test_spoken_date_sinhala_is_relative_to_today(value, expected) -> None:
    assert spoken_date(value, date(2026, 10, 15), "si") == expected  # 2026-10-15 is a Thursday


def test_spoken_date_english_unchanged() -> None:
    assert spoken_date("2026-10-19", date(2026, 10, 15), "en") == "Monday, 19 October"
