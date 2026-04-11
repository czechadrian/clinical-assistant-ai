"""
router.py — deterministic intent router for Kliniczny Asystent AI.

Maps (mode, input_text) → RouterDecision without any I/O or async calls.
Pure function — safe to import, test, and call without side effects.

Design: workflow-first, not agent-first.
  The clinician selects the mode; the router confirms it and annotates why.
  Heuristics NEVER override the mode — they only enrich router_reason for
  observability and post-hoc debugging.

Router rules (applied in order):
  1. mode drives the primary routing decision. It is always respected.
  2. For mode=triage only: text heuristics annotate router_reason to surface
     structural signals (completed note vs. question vs. standard triage).
     This is informational — the system does NOT redirect.
  3. doc_qa, summary, patient_message: mode-only routing, no text heuristics.

Example decisions:
  ("triage", "Patient 65M chest pain since 2h, diaphoresis")
    → RouterDecision("triage", "mode_triage")

  ("triage", "Rozpoznanie: OZW. Pacjent wypisany w dobie 5.")
    → RouterDecision("triage", "heuristic_note_pattern")
    (looks like a completed note — clinician may want summary mode instead)

  ("triage", "What is the recommended door-to-balloon time for STEMI?")
    → RouterDecision("triage", "heuristic_question_only")
    (direct question — clinician may want doc_qa instead)

  ("summary",  "Notatka z wizyty: ...")
    → RouterDecision("summary", "mode_summary")

  ("doc_qa", "What is the first-line treatment for HFrEF?")
    → RouterDecision("doc_qa", "mode_doc_qa")

Privacy note: heuristic patterns match structural keywords only.
They are never logged with the matched text — only router_reason is logged.
"""

import re
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

WorkflowName = Literal["triage", "summary", "patient_message", "doc_qa"]

# Stable codes — logged in metadata, never contain user content.
RouterReason = Literal[
    "mode_triage",
    "mode_summary",
    "mode_patient_message",
    "mode_doc_qa",
    "heuristic_note_pattern",  # triage input looks like a completed clinical note
    "heuristic_question_only",  # triage input is a single direct question (< 200 chars, ends "?")
]


@dataclass(frozen=True)
class RouterDecision:
    workflow: WorkflowName
    reason: RouterReason


# ---------------------------------------------------------------------------
# Heuristic patterns (triage only)
#
# _NOTE_KEYWORDS_RE: matches structural markers of a completed clinical note.
#   Keywords chosen for Polish clinical documentation conventions.
#   False-positive rate is acceptable — reason is informational only.
#
# _QUESTION_ONLY_RE: stripped text is a single question (ends "?", short).
#   Signals that doc_qa might be a better mode for this query.
# ---------------------------------------------------------------------------

_NOTE_KEYWORDS_RE = re.compile(
    r"\b("
    r"rozpoznanie|diagnoza|wypisany|wypisano"
    r"|hospitalizowany|hospitalizacja|przyjęty|przyjęto"
    r"|echokardiografia|EKG\ wykazało|RTG\ klatki|TK\ głowy"
    r"|wywiad:|badanie\ fizykalne:|zalecenia:|epikryza"
    r")\b",
    re.IGNORECASE,
)

# Short question: optionally prefixed text, ends "?", total stripped < 200 chars.
_QUESTION_ONLY_RE = re.compile(r"^\s*[^.!\n]{5,199}\?\s*$", re.DOTALL)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def route(mode: str, input_text: str) -> RouterDecision:
    """Map (mode, input_text) → RouterDecision.

    The clinician's mode is always honoured.
    Text heuristics annotate router_reason for triage inputs only.
    """
    if mode == "summary":
        return RouterDecision(workflow="summary", reason="mode_summary")

    if mode == "patient_message":
        return RouterDecision(workflow="patient_message", reason="mode_patient_message")

    if mode == "doc_qa":
        return RouterDecision(workflow="doc_qa", reason="mode_doc_qa")

    # mode == "triage" (also the fallback for any future unknown mode values
    # that pass Pydantic validation — belt-and-suspenders).
    if _NOTE_KEYWORDS_RE.search(input_text):
        return RouterDecision(workflow="triage", reason="heuristic_note_pattern")
    if _QUESTION_ONLY_RE.match(input_text):
        return RouterDecision(workflow="triage", reason="heuristic_question_only")
    return RouterDecision(workflow="triage", reason="mode_triage")
