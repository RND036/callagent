import inspect
from datetime import datetime

import pytest

from gemini_session.persona import (
    SYSTEM_INSTRUCTION,
    build_initial_greeting,
    build_system_instruction,
    normalize_language,
)
from gemini_session.live_session import build_connect_config
from gemini_session.persona import GOODBYE_RE, call_recap, greeting_note, missed_turns_note, resume_note

def test_persona_contains_sinhala_greeting():
    assert "ආයුබෝවන්! Classy Nail Spa වෙත ඔබව සාදරයෙන් පිළිගන්නවා." in SYSTEM_INSTRUCTION
    assert "මම ඔබගේ AI Receptionist. ඔබට අද මොන වගේ උදව්වක්ද අවශ්‍ය?" in SYSTEM_INSTRUCTION

def test_persona_uses_polite_spoken_sinhala():
    assert "SPOKEN SINHALA GUIDELINES" in SYSTEM_INSTRUCTION
    assert '"ඔබ", "ඔබගේ", "ඔබට"' in SYSTEM_INSTRUCTION
    assert "NEVER add a title to the caller" in SYSTEM_INSTRUCTION
    assert "නිපුනි මිස්" not in SYSTEM_INSTRUCTION  # no "sir" / "miss" after the caller's name
    assert 'NEVER use "ඔබගේ"' not in SYSTEM_INSTRUCTION  # the old casual-only ban is gone
    assert "NATURAL SALON ENGLISH MIXING" in SYSTEM_INSTRUCTION


@pytest.mark.parametrize("language", ["si", "ta", "en"])
def test_persona_bans_sir_and_miss_in_every_language(language):
    assert 'NEVER call the caller "sir"' in build_system_instruction(language=language)


def test_persona_has_the_scripts_four_rules_and_flows():
    for needle in (
        'NEVER say a booking is "booked" or "confirmed"',
        "NEVER guess a service, price, opening hour, address, policy or offer",
        "Ask the caller ONLY for what is needed",
        "Send complaints and complex treatment questions to human staff",
        "NEW BOOKING", "PRICE QUESTION", "RESCHEDULE", "COMPLAINT", "EXTENSION REMOVAL", "LOCATION",
    ):
        assert needle in SYSTEM_INSTRUCTION, needle


def test_persona_lists_branches_whatsapp_and_faq():
    from data.settings_store import SalonSettings

    text = build_system_instruction(
        SalonSettings(branches="Nugegoda, Hokandara", whatsapp_number="+94 77 111 2222"),
        faqs=[{"question": "Parking?", "answer": "Free parking behind the building.", "enabled": True},
              {"question": "Hidden?", "answer": "off", "enabled": False},
              {"question": "Empty?", "answer": "", "enabled": True}],
    )
    assert "Branches: Nugegoda (say it as: නුගේගොඩ), Hokandara (say it as: හොකන්දර)" in text and "+94 77 111 2222" in text
    assert "Q: Parking?\nA: Free parking behind the building." in text
    assert "Hidden?" not in text and "Empty?" not in text
    assert "No FAQ entries are set" in build_system_instruction()

def test_persona_locks_to_the_ivr_language():
    assert "CALL LANGUAGE: SINHALA (LOCKED" in SYSTEM_INSTRUCTION
    assert "Speak ONLY Sinhala for the ENTIRE call" in SYSTEM_INSTRUCTION

def test_greeting_note_is_in_the_ivr_language():
    assert "Sinhala only" in greeting_note(None, "si")
    assert "English only" in greeting_note(None, "en")


def test_call_recap_lists_turns_and_tools_and_is_empty_before_speech():
    assert call_recap([]) == ""
    recap = call_recap([{"role": "ai", "text": "Hi"}, {"role": "caller", "text": "book"},
                        {"role": "tool", "text": "create_booking -> ok"}])
    assert "You: Hi" in recap and "Caller: book" in recap and "Done: create_booking -> ok" in recap
    assert "Never run a tool again" in recap


def test_recap_lists_known_customer_details_and_keeps_the_language():
    recap = call_recap([{"role": "caller", "text": "hi"}], {"name": "Nimali", "booking_id": "B7"}, "ta")
    assert "- Name: Nimali" in recap and "- Booking ID: B7" in recap
    assert "Keep speaking Tamil only, in the same voice" in recap


def test_resume_notes_are_silent_and_in_the_call_language():
    note = resume_note("si")
    assert "Sinhala only" in note and "do not apologise" in note
    assert missed_turns_note([], "si") == ""
    missed = missed_turns_note([{"role": "caller", "text": "tomorrow 3pm"}], "en")
    assert "Caller: tomorrow 3pm" in missed and "English only" in missed


def test_goodbye_regex():
    assert GOODBYE_RE.search("Thank you, goodbye!")
    assert not GOODBYE_RE.search("What day would you like?")

def test_persona_forbids_asking_language():
    assert "NEVER ask" in SYSTEM_INSTRUCTION
    assert "DO NOT ASK" in SYSTEM_INSTRUCTION

def test_persona_has_no_auto_detect_switching_guard():
    # The IVR menu replaced language auto-detection: nothing about a Sinhala
    # default or switching after "more than 4 words" may remain.
    for language in ("si", "ta", "en"):
        instruction = build_system_instruction(language=language)
        assert "Sinhala is the DEFAULT" not in instruction
        assert "MORE THAN 4 WORDS" not in instruction
        assert "MID-CALL LANGUAGE SWITCHING" not in instruction

@pytest.mark.parametrize(
    "language, name, greeting",
    [
        ("ta", "TAMIL", "வணக்கம்! Classy Nail Spa-க்கு உங்களை அன்புடன் வரவேற்கிறோம்."),
        ("en", "ENGLISH", "Hello! Welcome to Classy Nail Spa. I'm your AI receptionist."),
    ],
)
def test_persona_for_tamil_and_english_calls(language, name, greeting):
    instruction = build_system_instruction(language=language)
    assert f"CALL LANGUAGE: {name} (LOCKED" in instruction
    assert greeting in instruction
    assert greeting in build_initial_greeting(language=language)
    # Sinhala speaking-style rules only matter on Sinhala calls.
    assert "NATIVE SPOKEN SINHALA GUIDELINES" not in instruction

def test_sinhala_mixing_note_kept_for_sinhala_and_tamil_calls():
    assert "සිංහලෙන් පමණක්" in SYSTEM_INSTRUCTION
    assert "தமிழில் மட்டும்" in build_system_instruction(language="ta")
    assert "never read tool output back in another language" in SYSTEM_INSTRUCTION

@pytest.mark.parametrize(
    "value, expected",
    [("si", "si"), ("ta", "ta"), ("en", "en"), ("EN", "en"), ("1", "si"), ("2", "ta"), ("3", "en"),
     ("", "si"), (None, "si"), ("9", "si"), ("fr", "si")],
)
def test_normalize_language(value, expected):
    assert normalize_language(value) == expected

@pytest.mark.parametrize(
    "language, codes", [("si", ["si-LK", "en-US"]), ("ta", ["ta-IN", "en-US"]), ("en", ["en-US"])]
)
def test_transcriber_only_listens_for_the_chosen_language(language, codes):
    config = build_connect_config(language=language)
    assert config.input_audio_transcription.language_codes == codes

def test_persona_restricts_to_salon_topics():
    assert "SCOPE: SALON TOPICS ONLY" in SYSTEM_INSTRUCTION
    assert "politely decline" in SYSTEM_INSTRUCTION
    assert "Do NOT answer the off-topic question" in SYSTEM_INSTRUCTION
    assert "do NOT use take_message or transfer_call for these" in SYSTEM_INSTRUCTION

def test_persona_allows_brief_pleasantries():
    assert "Brief, warm pleasantries are fine" in SYSTEM_INSTRUCTION


def test_persona_states_todays_date_and_caller_number():
    text = build_system_instruction(now=datetime(2026, 10, 13, 14, 5), caller_number="+94771234567")
    assert "Tuesday, 13 October 2026 (2026-10-13)" in text
    assert "14:05" in text
    assert "0771234567" in text and "බිංදුව හැත්තෑ හත, එකසිය විසි තුන, හතළිස් පහ, හැට හත" in text
    assert "2026-10-19 Monday = ලබන සතියේ සඳුදා, ඔක්තෝබර් දහනවවෙනිදා" in text and "ASK which day" in text
    assert 'NEVER say a booking is "booked" or "confirmed"' in text
    assert "never as numerals" in text and "customer_phone" in text

    anonymous = build_system_instruction(now=datetime(2026, 10, 13, 14, 5), caller_number="Browser Simulation")
    assert "CALLER'S NUMBER" not in anonymous


def test_persona_requires_one_consistent_voice():
    assert "KEEP EXACTLY THE SAME VOICE FOR THE WHOLE CALL" in SYSTEM_INSTRUCTION


def test_sinhala_prompt_gives_branch_names_in_native_script():
    si = build_system_instruction(language="si")
    assert "නුගේගොඩ" in si and "හොකන්දර" in si
    assert "නුගේගොඩ" not in build_system_instruction(language="en")


def test_unknown_branch_falls_back_to_latin_name():
    class S:
        branch_list = ["Nugegoda", "Colombo"]

    si = build_system_instruction(S(), language="si")
    assert "Colombo" in si and "Colombo (say it as" not in si


def test_price_only_when_asked():
    prompt = build_system_instruction(
        services=[{"name": "Gel manicure", "price_usd": 3500, "duration_minutes": 60}]
    )
    assert "PRICE ONLY WHEN ASKED" in prompt
    assert "- Gel manicure\n" in prompt and "Gel manicure (LKR" not in prompt


@pytest.mark.parametrize("language", ["si", "ta", "en"])
def test_persona_tells_caller_to_press_hash_to_change_language(language):
    instruction = build_system_instruction(language=language, language_menu_key="#")
    assert "CHANGING LANGUAGE" in instruction
    assert "press the # key" in instruction


def test_persona_without_a_menu_key_never_mentions_it():
    assert "CHANGING LANGUAGE" not in build_system_instruction(language="ta")
    assert "press the # key" not in SYSTEM_INSTRUCTION


def test_language_switched_note_names_the_language():
    from gemini_session.persona import language_switched_note

    assert "English" in language_switched_note("en")
    assert "Do not greet again" in language_switched_note("ta")


def test_address_is_spoken_in_native_script_on_si_and_ta_calls():
    si = build_system_instruction(language="si")
    assert "say it as: අංක තුන්සිය නවය, කෝට්ටේ පාර, නුගේගොඩ" in si
    ta = build_system_instruction(language="ta")
    assert "say it as: இலக்கம் முன்னூற்று ஒன்பது, கோட்டை வீதி, நுகேகொட" in ta
    en = build_system_instruction(language="en")
    assert "කෝට්ටේ" not in en and "say it as: இலக்கம்" not in en


def test_custom_spoken_address_reaches_the_sinhala_prompt():
    from data.settings_store import SalonSettings

    si = build_system_instruction(SalonSettings(salon_address_sinhala="අංක පහ, මල් පාර"), language="si")
    assert "say it as: අංක පහ, මල් පාර" in si


def test_new_booking_flow_confirms_name_and_phone():
    text = build_system_instruction()
    assert "review_booking" in text and "STOP; wait for the caller's answer" in text
    assert "CORRECTIONS (MANDATORY)" in text and "update_booking" in text
    assert "077 123 4567" not in text


def test_create_booking_requires_confirmed_phone():
    from gemini_session.tool_declarations import FUNCTION_DECLARATIONS

    decl = next(d for d in FUNCTION_DECLARATIONS if d["name"] == "create_booking")
    assert "customer_phone" in decl["parameters"]["required"]


def test_booking_is_followed_by_anything_else_and_never_ends_in_the_same_turn():
    text = build_system_instruction(language="si")
    assert "ඔබට වෙනත් යමක් දැනගැනීමට අවශ්‍යද?" in text
    assert "NEVER ask a question and call end_call in the same turn" in text
    from gemini_session.persona import ANYTHING_ELSE_RE

    assert ANYTHING_ELSE_RE.search("ඔබට වෙනත් යමක් දැනගැනීමට අවශ්‍යද?")
    assert ANYTHING_ELSE_RE.search("Is there anything else you'd like to know?")
    assert not ANYTHING_ELSE_RE.search("ස්තුතියි, සුබ දවසක් වේවා!")


def test_garbled_words_are_not_a_language_change():
    text = build_system_instruction(language="si", language_menu_key="#")
    assert "mis-heard" in text and "clear, full sentences" in text


def test_still_there_note_asks_if_they_can_hear_and_repeats_the_question():
    from gemini_session.persona import still_there_note

    note = still_there_note()
    assert "can hear you" in note and "repeat your last question" in note
