"""
Unit tests for redflag_detector.py (Day 24).

Coverage:
- True positives: one per category (6 cases)
- Mixed: multiple categories in one query
- False positive guards: common clinical text that must NOT trigger
- Edge cases: empty string, English terms, case-insensitivity
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from redflag_detector import RedFlagResult, detect_red_flags

# ---------------------------------------------------------------------------
# True positives — one representative phrase per category
# ---------------------------------------------------------------------------


def test_chest_pain_polish():
    r = detect_red_flags("Pacjent zgłasza ból w klatce piersiowej od 30 minut.")
    assert r.severity == "high"
    assert "chest_pain_dyspnea" in r.categories


def test_chest_pain_english():
    r = detect_red_flags("Patient presents with chest pain radiating to the left arm.")
    assert r.severity == "high"
    assert "chest_pain_dyspnea" in r.categories


def test_dyspnea_at_rest():
    r = detect_red_flags("Nagła duszność spoczynkowa, SpO2 88%.")
    assert r.severity == "high"
    assert "chest_pain_dyspnea" in r.categories


def test_stroke_signs():
    r = detect_red_flags("Udar mózgu podejrzany — asymetria twarzy, afazja.")
    assert r.severity == "high"
    assert "stroke_signs" in r.categories


def test_anaphylaxis():
    r = detect_red_flags("Wstrząs anafilaktyczny po podaniu antybiotyku.")
    assert r.severity == "high"
    assert "anaphylaxis" in r.categories


def test_suicidal_intent():
    r = detect_red_flags("Pacjent wyraża myśli samobójcze i planuje to dziś w nocy.")
    assert r.severity == "high"
    assert "suicidal_intent" in r.categories


def test_sepsis():
    r = detect_red_flags("Podejrzenie sepsy: gorączka 39,8°C, tachykardia, hipotonia.")
    assert r.severity == "high"
    assert "sepsis" in r.categories


def test_severe_bleeding():
    r = detect_red_flags("Masywny krwotok z przewodu pokarmowego, hemoglobina 5 g/dl.")
    assert r.severity == "high"
    assert "severe_bleeding" in r.categories


# ---------------------------------------------------------------------------
# Multiple categories in one query
# ---------------------------------------------------------------------------


def test_multiple_categories():
    r = detect_red_flags("Pacjent z OZW i podejrzeniem sepsy. Ból w klatce piersiowej, gorączka.")
    assert r.severity == "high"
    assert "chest_pain_dyspnea" in r.categories
    assert "sepsis" in r.categories
    assert len(r.categories) >= 2


# ---------------------------------------------------------------------------
# False positive guards — must NOT trigger
# ---------------------------------------------------------------------------


def test_no_fp_general_chest():
    # "ból" alone without anatomical qualifier should not fire
    r = detect_red_flags("Pacjent skarży się na ból głowy i zmęczenie od tygodnia.")
    assert r.severity == "none"
    assert r.categories == []


def test_no_fp_dyspnea_on_exertion():
    # "duszność" without "spoczynkowa" or "nagła" should not fire
    r = detect_red_flags("Duszność wysiłkowa przy wchodzeniu na schody — NYHA II.")
    assert r.severity == "none"


def test_no_fp_stroke_heat():
    # "udar cieplny" must not trigger — requires "udar mózgu"
    r = detect_red_flags("Udar cieplny po długim przebywaniu na słońcu.")
    assert r.severity == "none"


def test_no_fp_regular_bleeding():
    # Bare "krwawienie" without qualifier should not fire
    r = detect_red_flags("Niewielkie krwawienie z dziąseł po szczotkowaniu zębów.")
    assert r.severity == "none"


def test_no_fp_historical_attempt():
    # "próba samobójcza" in any context fires — safety-first by design
    # This is intentional: even historical mention warrants evaluation
    r = detect_red_flags("W wywiadzie próba samobójcza 5 lat temu.")
    assert r.severity == "high"
    assert "suicidal_intent" in r.categories


def test_no_fp_zabic_alone():
    # "zabić" in context like "ból mnie zabija" should NOT match our pattern
    # (our pattern requires full "chce się zabić" / "odebrać życie" etc.)
    r = detect_red_flags("Ten ból mnie zabija, trzeba coś zrobić.")
    assert r.severity == "none"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_string():
    r = detect_red_flags("")
    assert r.severity == "none"
    assert r.categories == []


def test_case_insensitive():
    r = detect_red_flags("BÓL W KLATCE PIERSIOWEJ od 2 godzin.")
    assert r.severity == "high"
    assert "chest_pain_dyspnea" in r.categories


def test_result_is_frozen():
    r = detect_red_flags("chest pain")
    assert isinstance(r, RedFlagResult)
    with pytest.raises((AttributeError, TypeError)):
        r.severity = "none"  # type: ignore[misc]


def test_explanation_contains_no_raw_text():
    text = "Pacjent: Jan Kowalski, ból w klatce piersiowej."
    r = detect_red_flags(text)
    # explanation must not contain the raw patient name
    assert "Jan Kowalski" not in r.explanation
    assert "Jan Kowalski" not in str(r.categories)
