"""
sanitizer.py — deterministic, regex-based PII/PHI detection and sanitization.

detect_and_sanitize(text) → SanitizerResult:
  pii_flags      — deduplicated list of detected category names (no raw matches)
  sanitized_text — original text with PII replaced by typed placeholders
  confidence     — dict mapping category → "high" | "medium"

Design principles:
- Deterministic: regex only, no ML, no network calls.
- Safety-first: prefer false positives over false negatives for high-confidence patterns
  (email, phone, PESEL, NIP). Medium-confidence patterns (address, date_of_birth)
  require an explicit context prefix to reduce noise in clinical notes.
- Replacements are typed placeholders — never user-supplied strings.
- Never returns raw matched substrings — only category labels and the sanitized copy.
- Side-effect-free: pure function, testable without FastAPI.
- Ordering matters: PESEL (11 consecutive digits) runs before phone (9-digit groups)
  so the digit suffix of a PESEL number cannot re-match as a phone number.

Supported categories and placeholders:
  email         → [EMAIL]
  phone         → [PHONE]
  pesel         → [PESEL]
  nip           → [NIP]
  date_of_birth → [DATE_OF_BIRTH]  (requires explicit DOB context prefix)
  address       → [ADDRESS]        (requires street-type prefix)

Usage:
  from sanitizer import detect_and_sanitize
  result = detect_and_sanitize("Call Jan at 604 123 456 or jan@example.com")
  # result.pii_flags  == ["phone", "email"]
  # result.sanitized_text == "Call Jan at [PHONE] or [EMAIL]"
  # result.confidence == {"phone": "high", "email": "high"}
"""

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SanitizerResult:
    """Immutable result of detect_and_sanitize.

    pii_flags      — deduplicated category names found (no raw matches, safe to log)
    sanitized_text — input with PII replaced by typed placeholders (show to user, do NOT store)
    confidence     — per-category confidence level: "high" | "medium"
    """

    pii_flags: list[str]
    sanitized_text: str
    confidence: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Detection + replacement rules
#
# Each entry: (category, confidence, placeholder, compiled_pattern)
#
# Order is significant:
#   1. PESEL  — 11 consecutive digits; must precede phone (9-digit groups).
#   2. Email  — contains @; unique, can run at any position.
#   3. NIP    — 10 digits with a specific dash layout; more specific than phone.
#   4. Phone  — 9 digits in groups; runs after PESEL to avoid digit-overlap.
#   5. Date of birth — requires an explicit context prefix to avoid flagging
#      ordinary clinical dates (exam dates, prescription dates, etc.).
#   6. Address — requires a Polish or English street-type prefix to avoid
#      flagging bare numbers or measurement values.
# ---------------------------------------------------------------------------

_RULES: list[tuple[str, str, str, re.Pattern[str]]] = [
    # 1. PESEL — exactly 11 consecutive digits with no adjacent digits.
    (
        "pesel",
        "high",
        "[PESEL]",
        re.compile(r"(?<!\d)\d{11}(?!\d)"),
    ),
    # 2. Email — requires @; ASCII-only avoids false positives in rich Unicode text.
    (
        "email",
        "high",
        "[EMAIL]",
        re.compile(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}", re.ASCII),
    ),
    # 3. NIP — Polish tax ID: xxx-xxx-xx-xx or xxx-xx-xx-xxx (10 digits with dashes).
    (
        "nip",
        "high",
        "[NIP]",
        re.compile(r"\b\d{3}-\d{2,3}-\d{2}-\d{2,3}\b"),
    ),
    # 4. Phone — Polish mobile/landline with optional +48 prefix and area code.
    #    Runs after PESEL so a PESEL placeholder cannot re-match here.
    (
        "phone",
        "high",
        "[PHONE]",
        re.compile(
            r"(?<!\d)"
            r"(?:\+48[\s\-]?)?"
            r"(?:\(?\d{2,3}\)?[\s.\-]?)?"
            r"\d{3}[\s.\-]?\d{3}[\s.\-]?\d{3}"
            r"(?!\d)"
        ),
    ),
    # 5. Date of birth — requires an explicit context keyword to avoid flagging
    #    clinical dates (admission dates, exam dates, etc.).
    #    Matches: "ur. 15.03.1980", "data urodzenia: 1980-03-15", "DOB 15/03/1980"
    (
        "date_of_birth",
        "medium",
        "[DATE_OF_BIRTH]",
        re.compile(
            r"\b(?:ur\.?|urodzony|urodzona|urodzeni|born|D\.?O\.?B\.?|data\s+urodzenia)"
            r"\s*:?\s*"
            r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}\b",
            re.IGNORECASE,
        ),
    ),
    # 6. Address — requires a Polish or English street-type prefix.
    #    Matches: "ul. Marszałkowska 10", "al. Jerozolimskie 44/2", "street Baker 221b"
    #    Does NOT match bare numbers or medical measurements.
    (
        "address",
        "medium",
        "[ADDRESS]",
        re.compile(
            r"\b(?:ul\.|ulica|al\.|aleja|pl\.|plac|os\.|osiedle|street|avenue|road|lane)"
            r"\s+\w[\w\s\-]{1,40}\d+\w*(?:/\d+\w*)?",
            re.IGNORECASE,
        ),
    ),
]


def detect_and_sanitize(text: str) -> SanitizerResult:
    """Replace PII in *text* with typed placeholders and return a SanitizerResult.

    Rules are applied sequentially; each replacement operates on the already-sanitized
    copy so a previously replaced placeholder cannot re-trigger a later pattern.

    The returned sanitized_text is intended for DISPLAY ONLY — it must NOT be stored
    automatically. The caller must require explicit user review and resubmission.

    Returns SanitizerResult(pii_flags=[], sanitized_text=text, confidence={}) for
    clean input — never raises.
    """
    sanitized = text
    found: dict[str, str] = {}  # category → confidence (preserves insertion order)

    for category, confidence, placeholder, pattern in _RULES:
        new_text, count = pattern.subn(placeholder, sanitized)
        if count > 0:
            sanitized = new_text
            if category not in found:
                found[category] = confidence

    return SanitizerResult(
        pii_flags=list(found.keys()),
        sanitized_text=sanitized,
        confidence=dict(found),
    )
