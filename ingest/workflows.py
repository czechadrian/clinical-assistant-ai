"""
workflows.py — mock workflow runners for Kliniczny Asystent AI.

Each workflow function:
  - Accepts a ClassifiedInput + optional rag_context string
  - Returns a raw dict matching the AssistantPayload schema
  - Has zero I/O — fully testable in isolation
  - Makes NO safety decisions (unsafe / vague / no-source handled upstream)

Sources are NOT populated here — main.py injects them from high_confidence
retrieval results after this call returns.

Week 3 LLM seam
---------------
Replace each function body with a real Claude call:

    system = SYSTEM_PROMPT + ("\\n\\n" + rag_context if rag_context else "")
    msg = await anthropic_client.messages.create(
        model="claude-opus-4-6",
        system=system,
        messages=[{"role": "user", "content": classified.for_llm()}],
    )
    return json.loads(msg.content[0].text)

The function signatures stay identical — only the body changes.

Workflow responsibilities
-------------------------
triage          Clarifying questions + red flags + conservative next steps.
                flag="uncertain" by default — triage always needs more info.
                Upgraded to "safe" in Week 3 when LLM provides a grounded answer.

summary         Structural transformation of a provided clinical note.
                No diagnosis claims.  flag="safe" — sources are optional enrichment.

patient_message Cautious patient-facing message draft for clinician review.
                flag="safe" — clinician reviews before sending.

doc_qa          Source-grounded answer.
                Precondition: main.py calls this ONLY when high_confidence sources
                exist (doc_qa no-source fallback fires earlier in main.py).
                In Week 3 the LLM composes rag_context into a precise answer.
"""

from typing import Any

from classifier import ClassifiedInput

# ---------------------------------------------------------------------------
# Disclaimer strings — stable, safe to log
# ---------------------------------------------------------------------------

_DISCLAIMER = (
    "Asystent AI nie zastępuje porady lekarskiej ani decyzji klinicznej. "
    "Zawsze konsultuj się z wykwalifikowanym specjalistą."
)

_PATIENT_DISCLAIMER = (
    "Te informacje mają charakter pomocniczy i nie zastępują indywidualnej porady lekarskiej. "
    "W nagłym przypadku zadzwoń pod numer alarmowy 112."
)


# ---------------------------------------------------------------------------
# Workflow functions
# ---------------------------------------------------------------------------


def run_triage(classified: ClassifiedInput, rag_context: str = "") -> dict[str, Any]:
    """TRIAGE: clarifying questions + red flags + conservative next steps.

    flag="uncertain": triage by definition needs more clinical information.
    In Week 3 with a real LLM and high-confidence sources, flag becomes "safe".
    """
    return {
        "questions_to_ask": [
            "Jak długo trwają objawy?",
            "Czy występują objawy alarmowe (ból w klatce piersiowej, duszność, utrata przytomności)?",
            "Jakie leki przyjmuje pacjent na stałe?",
        ],
        "red_flags": [
            "Ból w klatce piersiowej promieniujący do lewego ramienia lub szczęki — wykluczyć OZW.",
            "Nagła duszność spoczynkowa — pilna ocena układu oddechowego i krążenia.",
        ],
        "possible_next_steps": [
            "Zebranie pełnego wywiadu lekarskiego i badanie fizykalne.",
            "EKG 12-odprow. jeśli podejrzenie OZW.",
            "W przypadku objawów zagrażających życiu — natychmiastowe skierowanie na SOR.",
        ],
        "patient_facing_summary": (
            "Lekarz zbiera informacje o Twoich objawach, aby ocenić, jaka pomoc jest Ci potrzebna."
        ),
        "sources": [],  # populated by main.py after this call
        "flag": "uncertain",
        "disclaimer": _DISCLAIMER,
    }


def run_summary(classified: ClassifiedInput, rag_context: str = "") -> dict[str, Any]:
    """SUMMARY: structural transformation of a provided clinical note.

    No diagnosis claims.  flag="safe" — summary operates on provided text,
    not on model inference, so sources are optional enrichment not a requirement.
    """
    return {
        "questions_to_ask": [],
        "red_flags": [],
        "possible_next_steps": [
            "Przekazać podsumowanie kliniczne lekarzowi prowadzącemu.",
            "Zaktualizować dokumentację medyczną pacjenta.",
        ],
        "patient_facing_summary": (
            "Poniżej przedstawiono podsumowanie wizyty. W razie pytań prosimy o kontakt z lekarzem."
        ),
        "sources": [],  # populated by main.py if high_confidence sources found
        "flag": "safe",
        "disclaimer": _DISCLAIMER,
    }


def run_patient_message(classified: ClassifiedInput, rag_context: str = "") -> dict[str, Any]:
    """PATIENT_MESSAGE: cautious patient-facing message draft.

    flag="safe" — the clinician reviews and approves the message before it reaches the patient.
    """
    return {
        "questions_to_ask": [
            "Czy rozumiesz zalecenia lekarza i nie masz dodatkowych pytań?",
        ],
        "red_flags": [],
        "possible_next_steps": [
            "Przyjmuj leki zgodnie z zaleceniami — nie przerywaj terapii samodzielnie.",
            "Jeśli objawy się nasilają lub pojawiają się nowe — skontaktuj się z lekarzem.",
            "W nagłym przypadku zadzwoń pod numer alarmowy 112.",
        ],
        "patient_facing_summary": (
            "Twój lekarz przygotował dla Ciebie te informacje. "
            "Prosimy o zapoznanie się z nimi i przestrzeganie zaleceń."
        ),
        "sources": [],  # populated by main.py if high_confidence sources found
        "flag": "safe",
        "disclaimer": _PATIENT_DISCLAIMER,
    }


def run_doc_qa(classified: ClassifiedInput, rag_context: str = "") -> dict[str, Any]:
    """DOC_QA: source-grounded answer mock.

    Precondition (enforced by main.py): this function is only reached when
    high_confidence sources exist.  If sources are absent, the no-source
    fallback in main.py fires before this call and returns flag="uncertain".

    Week 3: rag_context (formatted by _format_rag_context) will be composed
    into the LLM prompt to produce a precise, source-grounded answer.
    """
    return {
        "questions_to_ask": [],
        "red_flags": [],
        "possible_next_steps": [],
        "patient_facing_summary": (
            "Odpowiedź przygotowana na podstawie zaindeksowanych wytycznych klinicznych. "
            "Szczegóły w sekcji źródeł poniżej."
        ),
        "sources": [],  # populated by main.py from high_confidence retrieval
        "flag": "safe",
        "disclaimer": _DISCLAIMER,
    }
