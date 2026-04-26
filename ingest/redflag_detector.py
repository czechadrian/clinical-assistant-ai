"""
redflag_detector.py — deterministic, keyword-based red flag detector for clinical triage.

Design:
- Pure function: text → RedFlagResult. Zero I/O, zero ML.
- Safety-first: when a phrase is ambiguous, include it (false negative > false positive).
- False-positive reduction: requires specific clinical collocations, not single words.
- Never stores or returns raw matched text — only category labels and counts.
"""

import re
from dataclasses import dataclass, field

Severity = str  # "none" | "high"

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RedFlagResult:
    severity: Severity  # "none" | "high"
    categories: list[str] = field(default_factory=list)
    explanation: str = "No red flags detected."

    @property
    def is_high(self) -> bool:
        return self.severity == "high"


# ---------------------------------------------------------------------------
# Category definitions
# Each entry: category_name → list of compiled patterns (OR within a category)
# ---------------------------------------------------------------------------


def _pats(*exprs: str) -> list[re.Pattern[str]]:
    return [re.compile(expr, re.IGNORECASE) for expr in exprs]


_CATEGORIES: dict[str, list[re.Pattern[str]]] = {
    # 1. Chest pain / acute coronary syndrome / sudden dyspnea
    #    Polish nouns inflect: "bólem" (instr.), "bólowi" (dat.), "dusznością" (instr.).
    #    Patterns use stem + \w* to cover all cases while requiring the anatomical qualifier.
    #    Bare "ból" or "duszność" alone do NOT fire.
    "chest_pain_dyspnea": _pats(
        r"\bból\w*\s+w\s+klatce\s+piersiowej\b",  # ból/bólem/bólowi w klatce piersiowej
        r"\bbóle?\w*\s+zamostkow\w+\b",  # ból/bóle zamostkowy/zamostkowe
        r"\bchest\s+pain\b",
        r"\bnagł\w+\s+dusznoś\w+\b",  # nagła/nagłą duszność/dusznością
        r"\bdusznoś\w+\s+spoczynkow\w+\b",  # duszność/dusznością spoczynkowa/spoczynkową
        r"\bostry\s+zawał\b",
        r"\bOZW\b",
        r"\bACS\b",
        r"\bNSTEMI\b",
        r"\bSTEMI\b",
        r"\bzawał\w*\s+serca\b",
    ),
    # 2. Stroke / TIA signs
    #    "udar" alone would match "udar cieplny" — require "mózgu" or neurological context.
    "stroke_signs": _pats(
        r"\budar\w*\s+mózgu\b",  # udar/udaru mózgu
        r"\bniedowład\w*\s+połowicz\w+\b",
        r"\bpołowicz\w+\s+niedowład\w*\b",
        r"\bafazj\w+\b",  # afazja/afazją
        r"\bnagł\w+\s+zaburzen\w+\s+mowy\b",
        r"\bnajsilniejszy\s+ból\w*\s+głowy\b",
        r"\bnagł\w+\s+ból\w*\s+głowy\b",
        r"\b(?:stroke|TIA)\b",
        r"\bprzemijaj\w+\s+napad\w*\s+niedokrwienny\b",
        r"\bnagł\w+\s+zaburzen\w+\s+widzenia\b",
        r"\bnagł\w+\s+zaburzen\w+\s+równowagi\b",
    ),
    # 3. Anaphylaxis / severe allergic reaction
    #    All terms are highly specific — no significant FP risk.
    "anaphylaxis": _pats(
        r"\banafilaksj\w+\b",  # anafilaksja/anafilaksją
        r"\bwstrząs\w*\s+anafilaktyczn\w+\b",
        r"\banaphylax\w+\b",
        r"\bobrzęk\w*\s+(?:quinckego|naczynioruchow\w+)\b",
        r"\bangioedema\b",
    ),
    # 4. Suicidal intent / self-harm
    #    "zabić" alone is a false positive; full phrases required.
    "suicidal_intent": _pats(
        r"\bmyśl\w+\s+samobójcz\w+\b",  # myśli samobójcze / myślach samobójczych
        r"\bzamiar\w*\s+samobójcz\w+\b",
        r"\bpróba\w*\s+samobójcz\w+\b",  # próba samobójcza / próby samobójczej
        r"\bsamobójstwo\b",
        r"\bchce?\s+(?:się\s+)?(?:skończyć\s+życie|odebrać\s+życie|zabić\s+się)\b",
        r"\bsuicidal\b",
        r"\bself[- ]harm\b",
        r"\bsamookaleczen\w+\b",
        r"\bsamokaleczen\w+\b",
        r"\bmyśl\w+\s+o\s+(?:śmierci|samobójstwie|odebraniu\s+sobie\s+życia)\b",
    ),
    # 5. Sepsis-like signs
    #    Polish inflects: "sepsy" (gen.), "sepsą" (instr.) — use stem seps\w+.
    "sepsis": _pats(
        r"\bseps\w+\b",  # sepsa/sepsy/sepsą/sepsie
        r"\bposocznic\w+\b",  # posocznica/posocznicy/posocznicą
        r"\bsepsis\b",
        r"\bwstrząs\w*\s+septyczn\w+\b",
        r"\bseptic\s+shock\b",
        r"\bDIC\b",
        r"\brozsiane\s+wykrzepianie\s+wewnątrznaczyniowe\b",
    ),
    # 6. Severe / uncontrolled bleeding
    #    Bare "krwawienie" is common and benign; "masywny" or "niepohamowane" required.
    "severe_bleeding": _pats(
        r"\bmasywn\w+\s+krwotok\w*\b",  # masywny/masywnym krwotok/krwotoku
        r"\bkrwotok\w*\s+masywn\w+\b",
        r"\bkrwotok\w*\s+(?:z\s+)?przewodu\s+pokarmowego\b",
        r"\bkrwotok\w*\s+wewnętrzn\w+\b",
        r"\bkrwawienie\w*\s+(?:nie\s+do\s+opanowania|niepohamowane\w*)\b",
        r"\bhemoptysis\b",
        r"\bkrwioplucie\b",
        r"\bmasywn\w+\s+krwawienie\w*\b",
        r"\bmassive\s+h[ae]morrhage\b",
        r"\bsevere\s+bleeding\b",
    ),
}

# ---------------------------------------------------------------------------
# Human-readable label for each category (safe for logs, never raw text)
# ---------------------------------------------------------------------------

CATEGORY_LABELS: dict[str, str] = {
    "chest_pain_dyspnea": "Ból w klatce piersiowej / nagła duszność — pilne wykluczenie OZW.",
    "stroke_signs": "Objawy neurologiczne — pilne wykluczenie udaru mózgu.",
    "anaphylaxis": "Objawy anafilaksji — natychmiastowa interwencja ratunkowa.",
    "suicidal_intent": "Myśli/zamiary samobójcze — natychmiastowa ocena psychiatryczna.",
    "sepsis": "Podejrzenie sepsy — pilna ocena i wdrożenie protokołu sepsowego.",
    "severe_bleeding": "Masywne krwawienie — pilna interwencja hemostazy.",
}

# Generic escalation steps injected when severity=high.
# Deliberately non-specific: no dosages, no diagnosis claims.
ESCALATION_STEPS: list[str] = [
    "PILNE: Wezwij zespół ratunkowy (112) lub skieruj pacjenta na SOR.",
    "Monitoruj parametry życiowe: BP, HR, SpO2, GCS — kontynuuj pomiar.",
    "Działaj natychmiast na podstawie obrazu klinicznego — nie czekaj na wyniki.",
    "Zapewnij dostęp dożylny i stabilizację jeśli wskazane klinicznie.",
]

URGENT_PATIENT_SUMMARY = (
    "PILNE: Wykryto objawy wymagające natychmiastowej oceny medycznej. "
    "Proszę o niezwłoczny kontakt z lekarzem lub wezwanie pogotowia ratunkowego (112)."
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_red_flags(text: str) -> RedFlagResult:
    """Scan *text* for high-severity clinical red flags.

    Returns RedFlagResult with severity='high' when at least one category matches.
    Pure function — no I/O, no side effects, testable without mocks.
    The explanation field contains only category names, never raw text.
    """
    matched: list[str] = []
    for category, patterns in _CATEGORIES.items():
        for pat in patterns:
            if pat.search(text):
                matched.append(category)
                break  # one match per category; check all categories

    if not matched:
        return RedFlagResult(severity="none", categories=[], explanation="No red flags detected.")

    return RedFlagResult(
        severity="high",
        categories=matched,
        explanation=(
            f"Red flags detected in {len(matched)} category/categories: {', '.join(matched)}."
        ),
    )
