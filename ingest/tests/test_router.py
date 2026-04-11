"""
Tests for router.py — deterministic intent router (Day 22).

Unit cases (pure function, no I/O):
  a) Each mode maps to its workflow
  b) Heuristic annotations for triage (note pattern, question-only)
  c) Heuristics do NOT redirect — workflow always matches mode
  d) Unknown/fallback mode → triage
  e) Router is deterministic (same input → same output)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from router import route

# ---------------------------------------------------------------------------
# a) Mode → workflow mapping (primary routing)
# ---------------------------------------------------------------------------


def test_triage_mode_routes_to_triage():
    d = route("triage", "Patient 65M chest pain since 2h, diaphoresis.")
    assert d.workflow == "triage"


def test_summary_mode_routes_to_summary():
    d = route("summary", "Notatka z wizyty ambulatoryjnej...")
    assert d.workflow == "summary"
    assert d.reason == "mode_summary"


def test_patient_message_mode_routes_to_patient_message():
    d = route("patient_message", "Proszę przyjmować lek raz dziennie.")
    assert d.workflow == "patient_message"
    assert d.reason == "mode_patient_message"


def test_doc_qa_mode_routes_to_doc_qa():
    d = route("doc_qa", "What is the door-to-balloon time target for STEMI?")
    assert d.workflow == "doc_qa"
    assert d.reason == "mode_doc_qa"


# ---------------------------------------------------------------------------
# b) Heuristic annotations for mode=triage
# ---------------------------------------------------------------------------


def test_triage_plain_input_reason_is_mode_triage():
    d = route("triage", "Patient 65M chest pain since 2h, diaphoresis.")
    assert d.reason == "mode_triage"


def test_triage_note_keywords_annotates_heuristic_note_pattern():
    note = "Rozpoznanie: OZW. Pacjent przyjęty w trybie pilnym. Wypisany w dobie 5."
    d = route("triage", note)
    assert d.workflow == "triage"  # still triage — heuristic never redirects
    assert d.reason == "heuristic_note_pattern"


def test_triage_note_keyword_case_insensitive():
    note = "DIAGNOZA: nadciśnienie tętnicze. Hospitalizacja 3 doby."
    d = route("triage", note)
    assert d.reason == "heuristic_note_pattern"


def test_triage_question_only_annotates_heuristic_question_only():
    d = route("triage", "What is the recommended door-to-balloon time for STEMI?")
    assert d.workflow == "triage"  # still triage
    assert d.reason == "heuristic_question_only"


def test_triage_question_only_short_polish():
    d = route("triage", "Jakie jest zalecane leczenie pierwszego rzutu w HFrEF?")
    assert d.reason == "heuristic_question_only"


# ---------------------------------------------------------------------------
# c) Heuristics never override mode
# ---------------------------------------------------------------------------


def test_summary_mode_with_note_keywords_still_summary():
    note = "Rozpoznanie: OZW. Hospitalizacja 5 dób. Zalecenia: ..."
    d = route("summary", note)
    assert d.workflow == "summary"
    assert d.reason == "mode_summary"


def test_doc_qa_mode_with_question_still_doc_qa():
    d = route("doc_qa", "What is the recommended door-to-balloon time?")
    assert d.workflow == "doc_qa"
    assert d.reason == "mode_doc_qa"


# ---------------------------------------------------------------------------
# d) Fallback for unknown mode
# ---------------------------------------------------------------------------


def test_unknown_mode_falls_back_to_triage():
    d = route("unknown_future_mode", "Some clinical input.")
    assert d.workflow == "triage"


# ---------------------------------------------------------------------------
# e) Determinism — same input always gives same output
# ---------------------------------------------------------------------------


def test_route_is_deterministic():
    inputs = [
        ("triage", "Chest pain."),
        ("summary", "Visit note."),
        ("doc_qa", "STEMI protocol?"),
        ("triage", "Rozpoznanie: OZW."),
    ]
    for mode, text in inputs:
        first = route(mode, text)
        second = route(mode, text)
        assert first == second, f"Non-deterministic for ({mode!r}, {text!r})"


# ---------------------------------------------------------------------------
# f) RouterDecision is frozen (immutable)
# ---------------------------------------------------------------------------


def test_router_decision_is_frozen():
    import dataclasses

    d = route("triage", "Chest pain.")
    assert dataclasses.is_dataclass(d)
    try:
        d.workflow = "summary"  # type: ignore[misc]
        raise AssertionError("Should have raised FrozenInstanceError")
    except Exception:
        pass  # expected
