"""
Input guardrails — personal data detection.

detect_pii(text) returns a human-readable label for the first pattern that
matches, or None if the text appears clean.

Rules:
- Patterns are conservative: we prefer a false-positive rejection (user removes
  the identifier and retries) over silently storing PII.
- This module never logs or returns the raw text — only the label.
- The caller is responsible for raising the HTTP error; this function is
  intentionally side-effect-free so it can be unit-tested without FastAPI.
"""

import re

# Each entry: (human-readable label, compiled pattern)
# Order matters — more specific patterns first.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
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


def detect_pii(text: str) -> str | None:
    """Return a label describing the PII type found, or None if text is clean."""
    for label, pattern in _PATTERNS:
        if pattern.search(text):
            return label
    return None
