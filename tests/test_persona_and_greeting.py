import inspect
from gemini_session.persona import SYSTEM_INSTRUCTION
from gemini_session.session import GeminiLiveSession

def test_persona_contains_sinhala_greeting():
    assert "ආයුබෝවන්! Classy Nail Spa වෙත සාදරයෙන් පිළිගනිමු." in SYSTEM_INSTRUCTION
    assert "මම ඔබට කොහොමද උදව් කරන්නේ?" in SYSTEM_INSTRUCTION

def test_persona_contains_language_detection_rules():
    assert "Sinhala" in SYSTEM_INSTRUCTION
    assert "Tamil" in SYSTEM_INSTRUCTION
    assert "English" in SYSTEM_INSTRUCTION
    assert "DETECT" in SYSTEM_INSTRUCTION or "detect" in SYSTEM_INSTRUCTION

def test_session_has_trigger_greeting_method():
    assert hasattr(GeminiLiveSession, "trigger_greeting")
    assert inspect.iscoroutinefunction(GeminiLiveSession.trigger_greeting)

def test_persona_forbids_asking_language():
    assert "NEVER ask" in SYSTEM_INSTRUCTION
    assert "DO NOT ASK" in SYSTEM_INSTRUCTION

def test_persona_symmetric_mid_call_language_switching():
    # Sinhala, Tamil, and English must all switch immediately and equally
    # mid-call -- no language (e.g. Sinhala) gets "sticky" default treatment.
    assert "EQUAL, ABSOLUTE strength to Sinhala, Tamil, and" in SYSTEM_INSTRUCTION
    assert "switch to match on the very next turn" in SYSTEM_INSTRUCTION
    assert "සිංහලෙන් පමණක්" in SYSTEM_INSTRUCTION
    assert "தமிழில் மட்டும்" in SYSTEM_INSTRUCTION
