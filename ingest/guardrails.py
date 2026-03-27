"""
Input guardrails — blocking checks that abort or re-route a request.

detect_pii(text)            — PII found → caller raises HTTP 400.
detect_unsafe_request(text) — out-of-safe-scope medical request → caller returns refuse payload.
is_vague_input(text)        — fewer than min_words → caller returns uncertain payload.

NOT in this module: injection-attempt detection.  Injection is non-blocking
(flag + continue) so it belongs in classifier.py, which composes all checks.

Rules:
- Never return the raw matched substring — only a human-readable label.
- Side-effect-free so every function is unit-testable without FastAPI.
"""

import re

# ---------------------------------------------------------------------------
# PII detection
# ---------------------------------------------------------------------------

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # E-mail
    (
        "e-mail address",
        re.compile(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}", re.ASCII),
    ),
    # Polish mobile / landline — optional +48, then 9 digits
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
    # PESEL — exactly 11 consecutive digits
    (
        "PESEL (11-digit national ID)",
        re.compile(r"(?<!\d)\d{11}(?!\d)"),
    ),
    # NIP — xxx-xxx-xx-xx or xxx-xx-xx-xxx
    (
        "NIP (tax ID)",
        re.compile(r"\b\d{3}-\d{2,3}-\d{2}-\d{2,3}\b"),
    ),
]

# Backwards-compat alias
_PATTERNS = _PII_PATTERNS


def detect_pii(text: str) -> str | None:
    """Return a label for the first PII pattern matched, or None if clean."""
    for label, pattern in _PII_PATTERNS:
        if pattern.search(text):
            return label
    return None


# ---------------------------------------------------------------------------
# Unsafe-request detection
#
# "Unsafe" means the user is explicitly asking for something the policy
# forbids: definitively diagnosing a patient or prescribing a drug.
#
# Injection attempts (ignore instructions, act as, etc.) are NOT unsafe
# requests — they are handled as non-blocking flags in classifier.py.
#
# Returns the category label, or None if the request is in scope.
# ---------------------------------------------------------------------------

_UNSAFE_REQUEST_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Definitive / confirmed diagnosis request
    # Catches: "confirm the diagnosis", "give a definitive diagnosis", "definitively diagnose"
    # Does NOT catch: "possible diagnosis", "differential diagnosis", "working diagnosis"
    (
        "diagnosis_request",
        re.compile(
            r"\b(confirm\s+(the\s+)?diagnos\w*"
            r"|definitive\s+diagnos\w*"
            r"|diagnos\w+\s+definitively"
            r"|potwierdź\s+(tę\s+)?diagnoz\w*"
            r"|zdiagnozuj\s+(definitywnie|ostatecznie)"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    # Prescription / drug ordering
    (
        "prescription_request",
        re.compile(
            r"\b(prescribe\b|wypisz\s+recept[ęe]|przepisz\s+lek)\b",
            re.IGNORECASE,
        ),
    ),
]


def detect_unsafe_request(text: str) -> str | None:
    """Return a category label if the request is medically out-of-scope, else None."""
    for label, pattern in _UNSAFE_REQUEST_PATTERNS:
        if pattern.search(text):
            return label
    return None


# ---------------------------------------------------------------------------
# Vague-input detection
# ---------------------------------------------------------------------------


def is_vague_input(text: str, min_words: int = 5) -> bool:
    """Return True when the input has fewer than min_words whitespace-separated tokens."""
    return len(text.split()) < min_words
