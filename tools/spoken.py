"""Pure helpers for saying things the way a local receptionist would on the phone: phone numbers, times and dates
as words, branch names in native script, and a classifier for the caller's answer to a read-back. The words come
from the language files (languages/*.yaml); this module is only the logic.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import languages


def _lang(code: str):
    langs = languages.languages()
    return langs.get(code) or langs["en"]


def spoken_branch(name: str, language: str = "en") -> str:
    """The branch as the voice should say it (native script where the language file has one); unknown branches come
    back unchanged."""
    return _lang(language).branches.get((name or "").strip().lower(), name)


def branch_from_native(text: object) -> str | None:
    """The English branch name for a native-script spelling ("නුගේගොඩ" -> "Nugegoda"), or None."""
    wanted = " ".join(str(text or "").split())
    for lang in languages.languages().values():
        for english, native in lang.branches.items():
            if wanted == native:
                return english.capitalize()
    return None


def local_number(raw: object) -> str | None:
    """A Sri Lankan number as 0XXXXXXXXX (from +94…, 94…, 0094… or 0…), or None if it is not a valid 10-digit one."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("0094"):
        digits = digits[4:]
    elif digits.startswith("94") and len(digits) == 11:
        digits = digits[2:]
    if len(digits) == 9 and not digits.startswith("0"):
        digits = "0" + digits
    return digits if len(digits) == 10 and digits.startswith("0") else None


def spoken_number(number: object, language: str = "en") -> str:
    """A phone number the way it is said on the phone: grouped as the language file says, said as numbers where the
    language has number words (Sinhala), otherwise digit by digit."""
    return _lang(language).numbers.say_phone(re.sub(r"\D", "", str(number or "")))


def spoken_time(value: str, language: str = "en") -> str:
    """"HH:MM" as said aloud. A language with a `time` block says it the local way (Sinhala: "උදේ එකොළහට", never
    AM/PM); others "11:00 AM"."""
    when = datetime.strptime(value, "%H:%M")
    clock = _lang(language).time
    if clock is None:
        return when.strftime("%I:%M %p").lstrip("0")
    numbers = _lang(language).numbers
    hour = clock.hours[when.hour % 12]
    part = next((p.name for p in reversed(clock.day_parts) if p.start <= when.hour), clock.day_parts[-1].name)
    if when.minute == 0:
        said = clock.exact.format(hour=hour)
    elif when.minute == 30:
        said = clock.half.format(hour=hour)
    else:
        said = clock.other.format(hour=hour, minutes=numbers.words[when.minute])
    return clock.layout.format(part=part, clock=said)


def spoken_date(value: str, today: date, language: str = "en") -> str:
    """"YYYY-MM-DD" as said aloud, relative to `today`. A language with a `dates` block says it the local way
    (Sinhala: "ලබන සතියේ සඳුදා, ඔක්තෝබර් දහනවවෙනිදා"); others "Monday, 19 October"."""
    day = datetime.strptime(value, "%Y-%m-%d").date()
    cal = _lang(language).dates
    if cal is None:
        return day.strftime("%A, %d %B")
    weekday = cal.weekdays[day.weekday()]
    calendar_day = cal.day_format.format(month=cal.months[day.month - 1], day=cal.day_ordinals[day.day - 1])
    delta = (day - today).days
    if delta in (0, 1, 2):
        return cal.relative_format.format(relative=cal.relative_days[delta], date=calendar_day)
    weeks_ahead = ((day.toordinal() - day.weekday()) - (today.toordinal() - today.weekday())) // 7
    if delta > 0 and weeks_ahead == 0:
        return cal.this_week.format(weekday=weekday, date=calendar_day)
    if delta > 0 and weeks_ahead == 1:
        return cal.next_week.format(weekday=weekday, date=calendar_day)
    return cal.other.format(weekday=weekday, date=calendar_day)


# Positive phrases that contain a negative word ("no problem") must not count as a "no".
def _reply_patterns() -> tuple[re.Pattern, re.Pattern, re.Pattern]:
    return languages.pattern("no_problem"), languages.pattern("no"), languages.pattern("yes")


def classify_reply(text: object) -> str:
    """"yes", "no" or "unclear" for the caller's answer to "is everything correct?". Any "no" wins."""
    no_problem, no, yes = _reply_patterns()
    cleaned, reassured = no_problem.subn(" ", str(text or ""))
    if no.search(cleaned):
        return "no"
    if reassured or yes.search(cleaned):
        return "yes"
    return "unclear"
