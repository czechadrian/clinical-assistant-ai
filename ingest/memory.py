"""
memory.py — deterministic, privacy-safe conversation memory builder.

build_memory(messages) → ConversationState

Design rules:
- Reads ONLY assistant message fields (questions_to_ask, possible_next_steps,
  red_flags, flag, _meta.workflow_name).  Raw user input text is never accessed,
  so patient-typed content cannot propagate into the persistent memory table.
- After building, _sanitize_or_clear runs over every output field.
  High-confidence PII (email / phone / PESEL / NIP) triggers a fail-safe:
  the field is emptied rather than partially redacted — data loss is safer than
  data leakage in a clinical context.
- Deterministic: regex only, no LLM, no network calls.
- Side-effect-free: pure function, testable without FastAPI.

Output shape (ConversationState):
  summary           — max 800 chars; clinical context without identifiers
  open_questions    — list[str]; last clarifying questions from the assistant
  known_constraints — list[str]; escalation red-flag labels
  updated_at        — ISO-format UTC timestamp
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sanitizer import SanitizerResult, detect_and_sanitize

_MAX_SUMMARY_CHARS = 800
_MAX_MESSAGES_SCANNED = 10  # look at the last N assistant messages only

# Categories where even a single match means the field must be cleared entirely.
# Medium-confidence categories (address, date_of_birth) are sanitized in-place.
_HIGH_CONFIDENCE_PII = frozenset({"email", "phone", "pesel", "nip"})


@dataclass
class ConversationState:
    """Sanitized clinical context for one conversation.

    Never instantiated with raw user input — only with assistant-generated
    content that has already been through _sanitize_or_clear.
    """

    summary: str
    open_questions: list[str] = field(default_factory=list)
    known_constraints: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def build_memory(messages: list[dict]) -> ConversationState:
    """Build a ConversationState from recent messages in a conversation.

    Only assistant messages are read.  User message text is intentionally
    ignored — raw clinical notes must not propagate into persistent memory.

    Returns an empty ConversationState when no assistant messages are present
    (brand-new conversation; caller can skip the upsert in this case).
    """
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"][-_MAX_MESSAGES_SCANNED:]

    if not assistant_msgs:
        return ConversationState(summary="", open_questions=[], known_constraints=[])

    open_questions: list[str] = []
    known_constraints: list[str] = []
    summary_parts: list[str] = []

    for msg in assistant_msgs:
        content = msg.get("content", {})
        if isinstance(content, str):
            # Legacy TEXT rows — skip; cannot parse safely here.
            continue

        flag: str = content.get("flag", "")
        red_flags: list = content.get("red_flags", [])
        questions: list = content.get("questions_to_ask", [])
        steps: list = content.get("possible_next_steps", [])
        meta: dict = content.get("_meta", {})
        workflow: str = meta.get("workflow_name", "")

        # Most-recent questions become the open_questions (overwrite each iteration
        # so the *last* assistant turn's questions surface in the summary).
        if questions:
            open_questions = [q for q in questions if isinstance(q, str)]

        # Accumulate red-flag labels as known constraints (deduped).
        for rf in red_flags:
            if isinstance(rf, str) and rf not in known_constraints:
                known_constraints.append(rf)

        # Build one summary fragment per assistant turn from controlled fields only.
        # First step is capped at 120 chars to keep fragments short.
        parts: list[str] = []
        if workflow:
            parts.append(f"[{workflow}]")
        if flag and flag != "safe":
            parts.append(f"flag:{flag}")
        if steps and isinstance(steps[0], str):
            parts.append(steps[0][:120])
        if parts:
            summary_parts.append(" ".join(parts))

    raw_summary = " | ".join(summary_parts)
    if len(raw_summary) > _MAX_SUMMARY_CHARS:
        raw_summary = raw_summary[: _MAX_SUMMARY_CHARS - 3] + "..."

    # PII safety net — runs AFTER construction, before any persistence.
    summary = _sanitize_or_clear(raw_summary)
    open_questions = [sanitized for q in open_questions if (sanitized := _sanitize_or_clear(q))]
    known_constraints = [
        sanitized for c in known_constraints if (sanitized := _sanitize_or_clear(c))
    ]

    return ConversationState(
        summary=summary,
        open_questions=open_questions,
        known_constraints=known_constraints,
    )


def _sanitize_or_clear(text: str) -> str:
    """Apply the PII sanitizer to *text* and return a safe version.

    High-confidence PII (email, phone, PESEL, NIP) → return "" (fail-safe).
    Medium-confidence PII (address, date_of_birth) → return the sanitized copy
      with placeholders ([ADDRESS], [DATE_OF_BIRTH]).
    Clean text → return unchanged.

    High-confidence PII in assistant-generated content is unexpected and likely
    indicates a data-quality bug upstream; emptying the field is safer than
    storing a partial redaction.
    """
    if not text.strip():
        return text
    result: SanitizerResult = detect_and_sanitize(text)
    if result.pii_flags and _HIGH_CONFIDENCE_PII.intersection(result.pii_flags):
        return ""
    return result.sanitized_text
