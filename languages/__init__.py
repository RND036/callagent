"""The languages the agent speaks: one file each in this folder (si.yaml, ta.yaml, en.yaml).

Every file has exactly the same keys (the Language model below). Everything that differs between languages lives
here: greetings, the lines the prompt quotes, speaking rules, goodbye / yes / no phrases, speech-to-text codes,
branch names and the way numbers, times and dates are said. Python keeps only the logic. A call speaks exactly one
of these languages (the caller picks it in the language menu).

A missing, misspelt or extra key, or an unknown {placeholder}, stops the app at startup with the file and the key
named. Adding a language means adding one file (and its menu key, which must be unique).
"""
from __future__ import annotations

import re
import string
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

LANG_DIR = Path(__file__).resolve().parent
# The order languages are listed in everywhere (prompt, dashboard, menu).
ORDER = ("si", "ta", "en")


class LanguageFileError(RuntimeError):
    pass


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*")
    @classmethod
    def _not_empty(cls, v):
        if isinstance(v, str) and not v.strip():
            raise ValueError("must not be empty")
        return v


class Lines(_Block):
    """Example lines the prompt quotes for the call's language."""

    delight: list[str]  # warm expressions
    change_language: str  # {key}: said when the caller asks for another language
    please_repeat: str
    out_of_scope: str  # {salon_name}
    anything_else: str  # asked after a booking request
    price_rule: str  # how to say prices
    phone_style: str  # how to say phone numbers


class Phrases(_Block):
    """Regex fragments (case-insensitive), OR-ed together across every language."""

    goodbye: list[str]  # the agent's goodbye
    anything_else: list[str]  # the agent asking "anything else?"
    yes: list[str]  # the caller's yes
    no: list[str]  # the caller's no
    no_problem: list[str]  # contain a "no" word but mean yes ("no problem")

    @field_validator("*")
    @classmethod
    def _regexes(cls, v):
        if not v or not all(str(p).strip() for p in v):
            raise ValueError("list at least one phrase, none empty")
        for p in v:
            try:
                re.compile(p)
            except re.error as exc:
                raise ValueError(f"'{p}' is not a valid regex: {exc}") from exc
        return v


class Numbers(_Block):
    digits: list[str]  # 0 to 9
    words: list[str]  # 0 to 99 (exactly 100); empty = phone numbers are said digit by digit
    hundreds: list[str]  # 100 to 900 when more follows (exactly 9)
    hundreds_alone: list[str]  # exactly 100 to 900 (exactly 9)
    phone_groups: list[int]  # a 10-digit phone number in groups, e.g. [3, 3, 4]

    @field_validator("digits", "words", "hundreds", "hundreds_alone")
    @classmethod
    def _word_lists(cls, v, info):
        want = {"digits": 10, "words": 100}.get(info.field_name, 9)
        if (v or info.field_name == "digits") and (len(v) != want or not all(str(x).strip() for x in v)):
            raise ValueError(f"needs exactly {want} words, none empty" + (" (or leave empty)" if want != 10 else ""))
        return [str(x).strip() for x in v]

    @field_validator("phone_groups")
    @classmethod
    def _groups(cls, v):
        if not v or any(n < 1 or n > 4 for n in v):
            raise ValueError("list group sizes from 1 to 4, e.g. [3, 3, 4]")
        return v

    def _group(self, digits: str) -> str:
        stripped = digits.lstrip("0")
        zeros = [self.words[0]] * (len(digits) - len(stripped))
        if not stripped:
            return " ".join(zeros)
        n = int(stripped)
        if n < 100:
            number = self.words[n]
        else:
            hundred, rest = divmod(n, 100)
            number = self.hundreds_alone[hundred - 1] if rest == 0 else f"{self.hundreds[hundred - 1]} {self.words[rest]}"
        return " ".join(zeros + [number])

    def say_phone(self, digits: str) -> str:
        """`digits` as a person says a phone number: a 10-digit number in groups said as numbers (if the language has
        number words) or digit by digit; anything else in groups of three digits."""
        if self.words and len(digits) == sum(self.phone_groups):
            parts, start = [], 0
            for size in self.phone_groups:
                parts.append(self._group(digits[start:start + size]))
                start += size
            return ", ".join(parts)
        groups = self.phone_groups if len(digits) == sum(self.phone_groups) else [3] * -(-len(digits) // 3)
        parts, start = [], 0
        for size in groups:
            if chunk := digits[start:start + size]:
                parts.append(" ".join(self.digits[int(d)] for d in chunk))
            start += size
        return ", ".join(parts)


class DayPart(_Block):
    start: int  # from this hour (0-23) until the next part starts
    name: str


class Clock(_Block):
    """Times said the local way. Without this block the time is written "2:00 PM"."""

    hours: list[str]  # the hour 12, 1 ... 11 (index = hour % 12)
    day_parts: list[DayPart]  # ascending by start; hours before the first belong to the last
    exact: str  # {hour}
    half: str  # {hour}
    other: str  # {hour} {minutes}
    layout: str  # {part} {clock}

    @field_validator("hours")
    @classmethod
    def _twelve(cls, v):
        if len(v) != 12 or not all(str(x).strip() for x in v):
            raise ValueError("needs exactly 12 hour names (12, 1 ... 11), none empty")
        return v

    @field_validator("day_parts")
    @classmethod
    def _parts(cls, v):
        if not v or [p.start for p in v] != sorted({p.start for p in v}) or not all(0 <= p.start < 24 for p in v):
            raise ValueError("list day parts by ascending start hour (0-23)")
        return v


class Calendar(_Block):
    """Dates said the local way. Without this block the date is written "Monday, 19 October"."""

    weekdays: list[str]  # Monday first
    months: list[str]
    day_ordinals: list[str]  # the 1st to the 31st
    relative_days: list[str]  # today, tomorrow, the day after
    day_format: str  # {month} {day}
    relative_format: str  # {relative} {date}
    this_week: str  # {weekday} {date}
    next_week: str  # {weekday} {date}
    other: str  # {weekday} {date}

    @field_validator("weekdays", "months", "day_ordinals", "relative_days")
    @classmethod
    def _sized(cls, v, info):
        want = {"weekdays": 7, "months": 12, "day_ordinals": 31, "relative_days": 3}[info.field_name]
        if len(v) != want or not all(str(x).strip() for x in v):
            raise ValueError(f"needs exactly {want} entries, none empty")
        return v


class Language(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    name: str  # in English, as the prompt says it ("Sinhala")
    native_name: str
    menu_digit: str  # the key an older dialplan menu sends for this language
    transcription_codes: list[str]
    greeting: str  # {salon_name}
    spoken_address: str  # the street address as the voice says it; "" = no special form
    branches: dict[str, str]  # lowercase English branch name -> how the voice says it
    currency_word: str
    mixing_rules: str
    style_rules: str  # {salon_name}; "" = none
    place_name_rules: str  # "" = none
    lines: Lines
    phrases: Phrases
    numbers: Numbers
    time: Clock | None
    dates: Calendar | None

    @field_validator("transcription_codes")
    @classmethod
    def _codes(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("list at least one speech-to-text code, e.g. si-LK")
        return v

    @field_validator("code", "name", "native_name", "menu_digit", "greeting", "currency_word", "mixing_rules")
    @classmethod
    def _filled(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("must not be empty")
        return v


# Placeholders each text may use; anything else is a typo. Texts not listed take none.
_ALLOWED = {
    "greeting": {"salon_name"}, "style_rules": {"salon_name"}, "lines.out_of_scope": {"salon_name"},
    "lines.change_language": {"key"},
    "time.exact": {"hour"}, "time.half": {"hour"}, "time.other": {"hour", "minutes"}, "time.layout": {"part", "clock"},
    "dates.day_format": {"month", "day"}, "dates.relative_format": {"relative", "date"},
    "dates.this_week": {"weekday", "date"}, "dates.next_week": {"weekday", "date"}, "dates.other": {"weekday", "date"},
}
_REQUIRED = {
    "lines.change_language": {"key"}, "time.exact": {"hour"}, "time.half": {"hour"}, "time.other": {"hour", "minutes"},
    "time.layout": {"part", "clock"}, "dates.day_format": {"month", "day"},
    "dates.relative_format": {"relative", "date"}, "dates.this_week": {"weekday", "date"},
    "dates.next_week": {"weekday", "date"}, "dates.other": {"weekday", "date"},
}


def fill(text: str, **values) -> str:
    """Fill the placeholders given in `values`; any other {placeholder} stays as it is."""

    class _Keep(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return string.Formatter().vformat(text, (), _Keep(values))


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def _texts(obj, where=""):
    """Every (dotted path, text) in a validated model, skipping regex phrases and branch names."""
    if isinstance(obj, str):
        yield where, obj
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            if name not in ("phrases", "branches"):
                yield from _texts(getattr(obj, name), f"{where}.{name}" if where else name)
    elif isinstance(obj, list):
        for item in obj:
            yield from _texts(item, where)


def _check_placeholders(lang: Language, file: str) -> None:
    for where, text in _texts(lang):
        allowed = _ALLOWED.get(where, set())
        try:
            used = _placeholders(text)
        except ValueError as exc:
            raise LanguageFileError(f"{file}: {where}: {exc} (use {{ }} only around placeholders)") from exc
        if unknown := used - allowed:
            raise LanguageFileError(f"{file}: {where}: unknown placeholder {{{sorted(unknown)[0]}}} "
                                    f"(allowed: {', '.join('{' + a + '}' for a in sorted(allowed)) or 'none'})")
        if missing := _REQUIRED.get(where, set()) - used:
            raise LanguageFileError(f"{file}: {where}: must contain {{{sorted(missing)[0]}}}")


def _describe(err: ValidationError, file: str) -> str:
    first = err.errors()[0]
    where = ".".join(str(p) for p in first["loc"]) or "(file)"
    kind = first["type"]
    if kind == "missing":
        return f"{file}: missing key '{where}'"
    if kind == "extra_forbidden":
        return f"{file}: unknown key '{where}' (misspelt?)"
    return f"{file}: {where}: {first['msg']}"


def load_file(path: Path) -> Language:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LanguageFileError(f"{path.name}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise LanguageFileError(f"{path.name}: expected keys like 'code:' and 'name:'")
    try:
        lang = Language.model_validate(data)
    except ValidationError as exc:
        raise LanguageFileError(_describe(exc, path.name)) from exc
    if lang.code != path.stem:
        raise LanguageFileError(f"{path.name}: code is '{lang.code}' but the file is named {path.name}")
    _check_placeholders(lang, path.name)
    return lang


def load_languages(directory: Path = LANG_DIR) -> dict[str, Language]:
    """Every language file, checked, in ORDER (others after, by code)."""
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise LanguageFileError(f"no language files in {directory}")
    langs = [load_file(f) for f in files]
    seen: dict[str, str] = {}
    for lang in langs:
        if lang.menu_digit in seen:
            raise LanguageFileError(f"{lang.code}.yaml: menu_digit '{lang.menu_digit}' is already used by {seen[lang.menu_digit]}.yaml")
        seen[lang.menu_digit] = lang.code
    langs.sort(key=lambda l: (ORDER.index(l.code) if l.code in ORDER else len(ORDER), l.code))
    return {lang.code: lang for lang in langs}


@lru_cache
def languages() -> dict[str, Language]:
    return load_languages()


def codes() -> list[str]:
    return list(languages())


def get(code: str) -> Language:
    return languages()[code]


def pattern(kind: str) -> re.Pattern:
    """One regex matching any language's phrases of this kind (a field of Phrases), case-insensitive."""
    parts = [p for lang in languages().values() for p in getattr(lang.phrases, kind)]
    return re.compile("|".join(parts) or r"(?!)", re.IGNORECASE)
