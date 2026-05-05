"""
Unit tests for memory.py — deterministic conversation memory builder.

Privacy invariants under test:
  - User message text is NEVER read (only assistant fields)
  - High-confidence PII (email/phone/PESEL/NIP) clears the field entirely
  - Medium-confidence PII (address) is replaced by placeholder
  - summary is capped at 800 characters
  - Empty / no-assistant-message input → empty state
  - Output structure is always ConversationState with the correct fields
"""

from memory import ConversationState, _sanitize_or_clear, build_memory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DISCLAIMER = "Niniejsza odpowiedź ma charakter wyłącznie informacyjny."


def _assistant_msg(
    questions: list[str] | None = None,
    steps: list[str] | None = None,
    red_flags: list[str] | None = None,
    flag: str = "uncertain",
    workflow: str = "triage",
) -> dict:
    return {
        "role": "assistant",
        "content": {
            "questions_to_ask": questions or [],
            "possible_next_steps": steps or [],
            "red_flags": red_flags or [],
            "patient_facing_summary": "",
            "sources": [],
            "flag": flag,
            "disclaimer": DISCLAIMER,
            "_meta": {"workflow_name": workflow, "router_reason": f"mode_{workflow}"},
        },
    }


def _user_msg(text: str) -> dict:
    return {"role": "user", "content": {"text": text, "mode": "triage"}}


# ---------------------------------------------------------------------------
# Empty / no-assistant cases
# ---------------------------------------------------------------------------


def test_build_memory_empty_list():
    state = build_memory([])
    assert isinstance(state, ConversationState)
    assert state.summary == ""
    assert state.open_questions == []
    assert state.known_constraints == []


def test_build_memory_only_user_messages():
    msgs = [_user_msg("Pacjent z gorączką."), _user_msg("Temperatura 38.5")]
    state = build_memory(msgs)
    assert state.summary == ""
    assert state.open_questions == []
    assert state.known_constraints == []


# ---------------------------------------------------------------------------
# Structure: correct fields are extracted from assistant messages
# ---------------------------------------------------------------------------


def test_build_memory_extracts_open_questions():
    msgs = [
        _assistant_msg(questions=["Od jak dawna?", "Jakie leki?"]),
    ]
    state = build_memory(msgs)
    assert "Od jak dawna?" in state.open_questions
    assert "Jakie leki?" in state.open_questions


def test_build_memory_open_questions_from_last_turn():
    """open_questions must reflect the most recent assistant turn, not the first."""
    msgs = [
        _assistant_msg(questions=["Stare pytanie 1", "Stare pytanie 2"]),
        _assistant_msg(questions=["Nowe pytanie 1"]),
    ]
    state = build_memory(msgs)
    assert state.open_questions == ["Nowe pytanie 1"]
    assert "Stare pytanie 1" not in state.open_questions


def test_build_memory_extracts_known_constraints_from_red_flags():
    msgs = [_assistant_msg(red_flags=["Ból w klatce piersiowej", "Duszność"])]
    state = build_memory(msgs)
    assert "Ból w klatce piersiowej" in state.known_constraints
    assert "Duszność" in state.known_constraints


def test_build_memory_deduplicates_known_constraints():
    msgs = [
        _assistant_msg(red_flags=["Ból w klatce piersiowej"]),
        _assistant_msg(red_flags=["Ból w klatce piersiowej"]),
    ]
    state = build_memory(msgs)
    assert state.known_constraints.count("Ból w klatce piersiowej") == 1


def test_build_memory_summary_contains_workflow_tag():
    msgs = [_assistant_msg(steps=["Wykonaj EKG"], workflow="triage")]
    state = build_memory(msgs)
    assert "[triage]" in state.summary


def test_build_memory_summary_contains_flag_when_not_safe():
    msgs = [_assistant_msg(flag="uncertain", steps=["Obserwuj pacjenta"])]
    state = build_memory(msgs)
    assert "flag:uncertain" in state.summary


def test_build_memory_summary_omits_flag_when_safe():
    msgs = [_assistant_msg(flag="safe", steps=["Kontynuuj leczenie"])]
    state = build_memory(msgs)
    assert "flag:safe" not in state.summary


# ---------------------------------------------------------------------------
# Privacy: user message text must never appear in output
# ---------------------------------------------------------------------------


def test_build_memory_user_text_never_in_summary():
    """Raw user input must not leak into the memory summary."""
    sensitive_user_text = "Pacjent Jan Kowalski PESEL 92010112345"
    msgs = [
        _user_msg(sensitive_user_text),
        _assistant_msg(steps=["Skierowanie do specjalisty"], questions=["Od kiedy?"]),
    ]
    state = build_memory(msgs)
    assert sensitive_user_text not in state.summary
    assert sensitive_user_text not in " ".join(state.open_questions)
    assert sensitive_user_text not in " ".join(state.known_constraints)
    # PESEL digits must not appear in any field
    assert "92010112345" not in state.summary
    assert "92010112345" not in " ".join(state.open_questions)


# ---------------------------------------------------------------------------
# Length cap
# ---------------------------------------------------------------------------


def test_build_memory_summary_max_800_chars():
    long_step = "A" * 500
    msgs = [_assistant_msg(steps=[long_step]) for _ in range(5)]
    state = build_memory(msgs)
    assert len(state.summary) <= 800


def test_build_memory_summary_truncated_with_ellipsis():
    long_step = "B" * 200
    msgs = [_assistant_msg(steps=[long_step]) for _ in range(5)]
    state = build_memory(msgs)
    if len(state.summary) == 800:
        assert state.summary.endswith("...")


# ---------------------------------------------------------------------------
# PII sanitizer safety net
# ---------------------------------------------------------------------------


def test_sanitize_or_clear_returns_empty_for_email():
    result = _sanitize_or_clear("Kontakt: jan@klinika.pl")
    assert result == ""


def test_sanitize_or_clear_returns_empty_for_phone():
    result = _sanitize_or_clear("Zadzwoń pod 604 123 456")
    assert result == ""


def test_sanitize_or_clear_returns_empty_for_pesel():
    result = _sanitize_or_clear("PESEL: 92010112345")
    assert result == ""


def test_sanitize_or_clear_returns_empty_for_nip():
    result = _sanitize_or_clear("NIP: 123-456-78-90")
    assert result == ""


def test_sanitize_or_clear_replaces_address_with_placeholder():
    result = _sanitize_or_clear("Zamieszkały przy ul. Marszałkowska 10")
    assert "[ADDRESS]" in result
    assert "Marszałkowska 10" not in result
    assert result != ""  # not cleared entirely (medium-confidence)


def test_sanitize_or_clear_clean_text_unchanged():
    text = "Ból głowy od dwóch dni, bez gorączki."
    assert _sanitize_or_clear(text) == text


def test_sanitize_or_clear_empty_string():
    assert _sanitize_or_clear("") == ""
    assert _sanitize_or_clear("   ") == "   "


def test_build_memory_clears_open_question_with_pii():
    """If an assistant question somehow contains an email, that question is dropped."""
    msgs = [
        _assistant_msg(
            questions=["Czy możemy wysłać wyniki na jan@klinika.pl?", "Od kiedy objawy?"]
        ),
    ]
    state = build_memory(msgs)
    assert all("@" not in q for q in state.open_questions)
    # Clean question survives
    assert "Od kiedy objawy?" in state.open_questions


def test_build_memory_clears_constraint_with_pii():
    """Red-flag label containing a phone number is dropped rather than stored."""
    msgs = [
        _assistant_msg(red_flags=["Zadzwoń na 112 lub 604 123 456", "Silny ból w klatce"]),
    ]
    state = build_memory(msgs)
    # Entry with phone is cleared; clean entry survives
    assert "Silny ból w klatce" in state.known_constraints
    assert all("604" not in c for c in state.known_constraints)


# ---------------------------------------------------------------------------
# Scan window limit
# ---------------------------------------------------------------------------


def test_build_memory_scans_at_most_10_messages():
    """Only the last 10 assistant messages are included in the summary."""
    msgs = [_assistant_msg(steps=[f"Krok {i}"], workflow="triage") for i in range(15)]
    state = build_memory(msgs)
    # Fragments are oldest-first within the 10-message window.
    # Step 5 (index 5) should be the first included (indices 5–14 → last 10).
    assert "Krok 4" not in state.summary  # message 4 is outside the window
    assert "Krok 5" in state.summary  # message 5 is the oldest in the window


# ---------------------------------------------------------------------------
# updated_at
# ---------------------------------------------------------------------------


def test_build_memory_has_updated_at():
    state = build_memory([_assistant_msg()])
    assert state.updated_at  # non-empty ISO string
    assert "T" in state.updated_at  # basic ISO format sanity check
