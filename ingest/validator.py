"""
Response validation and single-pass deterministic repair.

Flow in /chat:
    raw_dict = _build_raw_payload(...)         # from mock or (Week 2) real LLM
    payload, repair_applied = validate_and_repair(raw_dict, AssistantPayload)
    # raises pydantic.ValidationError if unrepairable → caller raises HTTP 500

Design constraints:
- One repair pass only — no loops, no second LLM calls.
- repair_payload() is pure (no I/O, no side effects) — unit-testable in isolation.
- validate_and_repair() is model-agnostic; pass any Pydantic BaseModel subclass.
- Neither function logs or returns raw invalid content.
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from constants import DISCLAIMER_CLINICAL

T = TypeVar("T", bound=BaseModel)

_FALLBACK_DISCLAIMER = DISCLAIMER_CLINICAL
_FALLBACK_SUMMARY = "Informacje o wizycie zostały przetworzone przez system."
_MAX_STR_LEN = 2000

_VALID_FLAGS = frozenset({"safe", "uncertain", "refuse"})

# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def repair_payload(data: dict[str, Any]) -> dict[str, Any]:
    """
    Apply deterministic one-pass coercions to a malformed assistant payload dict.
    Returns a new dict; the input is never mutated.

    What IS repaired (common LLM mistakes):
      - null/None list field  → []
      - string list field     → [string]
      - list items            → coerced to str; None items dropped
      - null/non-string patient_facing_summary → fallback string
      - string > _MAX_STR_LEN → trimmed
      - null sources          → []
      - sources item dict with missing keys → missing keys filled with ""
      - invalid string flag   → "uncertain"  (safe fallback)
      - null/empty disclaimer → fallback string

    What is NOT repaired (caller receives ValidationError → HTTP 500):
      - flag that is a non-string type (int, list, dict)
      - sources items that are not dicts (dropped, reducing sources to [])
    """
    out = dict(data)

    # ── list fields ──────────────────────────────────────────────────────────
    for key in ("questions_to_ask", "red_flags", "possible_next_steps"):
        val = out.get(key)
        if val is None:
            out[key] = []
        elif isinstance(val, str):
            out[key] = [val] if val.strip() else []
        elif isinstance(val, list):
            out[key] = [str(item) for item in val if item is not None]
        # Non-list, non-string, non-None → leave for validator to reject

    # ── patient_facing_summary ───────────────────────────────────────────────
    pfs = out.get("patient_facing_summary")
    if not isinstance(pfs, str) or not pfs.strip():
        out["patient_facing_summary"] = _FALLBACK_SUMMARY
    elif len(pfs) > _MAX_STR_LEN:
        out["patient_facing_summary"] = pfs[:_MAX_STR_LEN]

    # ── sources ──────────────────────────────────────────────────────────────
    sources = out.get("sources")
    if sources is None:
        out["sources"] = []
    elif isinstance(sources, list):
        coerced = []
        for item in sources:
            if isinstance(item, dict):
                coerced.append(
                    {
                        "id": str(item.get("id", "")),
                        "title": str(item.get("title", "")),
                        "section": str(item.get("section", "")),
                        "text_snippet": item.get(
                            "text_snippet"
                        ),  # None if absent — matches Source default
                    }
                )
            # Non-dict items (strings, ints) are dropped — cannot be coerced to Source.
        out["sources"] = coerced
    else:
        out["sources"] = []

    # ── flag ─────────────────────────────────────────────────────────────────
    flag = out.get("flag")
    if flag not in _VALID_FLAGS:
        if isinstance(flag, str):
            # An unrecognised string (e.g. "SAFE", "unknown") → safe fallback.
            out["flag"] = "uncertain"
        # Non-string types (int, dict, list) are intentionally left untouched.
        # The post-repair validation will reject them, producing a 500.

    # ── disclaimer ───────────────────────────────────────────────────────────
    disclaimer = out.get("disclaimer")
    if not isinstance(disclaimer, str) or not disclaimer.strip():
        out["disclaimer"] = _FALLBACK_DISCLAIMER
    elif len(disclaimer) > _MAX_STR_LEN:
        out["disclaimer"] = disclaimer[:_MAX_STR_LEN]

    return out


# ---------------------------------------------------------------------------
# Validate + repair
# ---------------------------------------------------------------------------


def validate_and_repair(
    data: dict[str, Any],
    model: type[T],
) -> tuple[T, bool]:
    """
    Validate `data` against `model`.  If invalid, apply one repair pass and
    validate again.  Returns ``(validated_instance, repair_applied)``.

    Raises ``pydantic.ValidationError`` if still invalid after repair.
    The caller is responsible for converting this to a safe HTTP 500.
    """
    try:
        return model.model_validate(data), False
    except ValidationError:
        pass

    repaired = repair_payload(data)
    # Raises ValidationError if unrepairable — intentionally not caught here.
    return model.model_validate(repaired), True
