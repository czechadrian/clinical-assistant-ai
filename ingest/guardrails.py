"""
Input guardrails — personal data detection, unsafe request detection, and vague input detection.

detect_pii(text)            — returns a label for the first PII pattern matched, or None.
detect_unsafe_request(text) — returns a label if the request is out-of-safe-scope, or None.
is_vague_input(text)        — True when the input is too short to produce a useful answer.

Rules:
- Patterns are conservative: we prefer a false-positive rejection over silently storing PII.
- This module never logs or returns the raw text — only the label.
- The caller is responsible for raising the HTTP error or building the fallback payload;
  these functions are intentionally side-effect-free so they can be unit-tested without FastAPI.
"""

import re

# ---------------------------------------------------------------------------
# PII detection
# ---------------------------------------------------------------------------

# Each entry: (human-readable label, compiled pattern)
# Order matters — more specific patterns first.
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # E-mail  — virtually no false positives
    (
        "e-mail address",
        re.compile(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}", re.ASCII),
    ),
    # Polish mobile / landline: optional country code, then 9 digits in common groupings
    # e.g.  +48 600 100 200  |  600-100-200  |  (22) 123 45 67
    (
        "phone number",
        re.compile(
            r"(?<!\d)"
            r"(\+48[\s\-]?)?"
            r"(\(?\d{2,3}\)?[\s.\-]?)?"
            r"\d{3}[\s.\-]?\d{3}[\s.\-]?\d{3}"
            r"(?!\d)"
        ),
    ),
    # PESEL — exactly 11 consecutive digits (Polish national ID)
    (
        "PESEL (11-digit national ID)",
        re.compile(r"(?<!\d)\d{11}(?!\d)"),
    ),
    # NIP (Polish tax ID) — formatted: xxx-xxx-xx-xx  or  xxx-xx-xx-xxx
    (
        "NIP (tax ID)",
        re.compile(r"\b\d{3}-\d{2,3}-\d{2}-\d{2,3}\b"),
    ),
]

# Keep the old name for backwards compatibility with existing code.
_PATTERNS = _PII_PATTERNS


def detect_pii(text: str) -> str | None:
    """Return a label describing the PII type found, or None if text is clean."""
    for label, pattern in _PII_PATTERNS:
        if pattern.search(text):
            return label
    return None


# ---------------------------------------------------------------------------
# Unsafe request detection
#
# Catches two categories:
#   1. Prompt injection — attempts to override the system policy.
#   2. Out-of-scope requests — definitively diagnosing or prescribing, which
#      the policy explicitly forbids and the model must refuse.
#
# Returns a human-readable label (for logging) or None if the request is safe.
# ---------------------------------------------------------------------------

_UNSAFE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Prompt injection — English: "ignore all previous instructions", "act as", etc.
    (
        "prompt injection attempt",
        re.compile(
            r"\bignore\s+(all\s+)?(previous\s+)?(instructions?|rules?|prompts?)\b", re.IGNORECASE
        ),
    ),
    (
        "prompt injection attempt",
        re.compile(
            r"\b(you are now|act as|pretend (you are|to be)|new role|reset your (instructions?|rules?))\b",
            re.IGNORECASE,
        ),
    ),
    # Prompt injection — Polish: "zignoruj instrukcje", "zapomnij o zasadach"
    (
        "prompt injection attempt",
        re.compile(
            r"\b(zignoruj|zapomnij|omiń|pomiń)\b.{0,25}\b(instrukcj[eę]|zasad[yę]|poleceni[ae]|reguł[yę])\b",
            re.IGNORECASE,
        ),
    ),
    # Definitive diagnosis confirmation request
    (
        "diagnosis confirmation request",
        re.compile(r"\b(confirm|potwierdź)\s+(the\s+)?diagnos(is|[eę])\b", re.IGNORECASE),
    ),
    # Prescription / drug ordering request
    (
        "prescription request",
        re.compile(r"\b(prescribe\b|wypisz\s+recept[ęe]|przepisz\s+lek)\b", re.IGNORECASE),
    ),
]


def detect_unsafe_request(text: str) -> str | None:
    """Return a label if the request is unsafe/out-of-scope, or None if it is safe."""
    for label, pattern in _UNSAFE_PATTERNS:
        if pattern.search(text):
            return label
    return None


# ---------------------------------------------------------------------------
# Vague input detection
#
# A request with fewer than `min_words` words cannot be answered meaningfully.
# The caller should return an "uncertain" payload with clarifying questions.
# ---------------------------------------------------------------------------


def is_vague_input(text: str, min_words: int = 5) -> bool:
    """Return True when the input has fewer than min_words whitespace-separated tokens."""
    return len(text.split()) < min_words
