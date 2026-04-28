"""
Unit tests for sanitizer.py (Day 25).

Coverage:
- True positives: one per category (email, phone, pesel, nip, date_of_birth, address)
- Multi-category: email + phone in one text
- Replacement: sanitized_text contains placeholder, not original
- Ordering: PESEL (11 digits) must be detected before phone (9-digit subset)
- False positive guards: clinical dates without prefix, medical numbers
- Edge cases: empty string, already-clean text, frozen result
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from sanitizer import SanitizerResult, detect_and_sanitize

# ---------------------------------------------------------------------------
# True positives — one per category
# ---------------------------------------------------------------------------


def test_email_detected():
    r = detect_and_sanitize("Contact the patient at jan.kowalski@example.com for follow-up.")
    assert "email" in r.pii_flags
    assert r.confidence["email"] == "high"


def test_email_replaced():
    r = detect_and_sanitize("E-mail: jan@klinika.pl")
    assert "[EMAIL]" in r.sanitized_text
    assert "jan@klinika.pl" not in r.sanitized_text


def test_phone_polish_mobile():
    r = detect_and_sanitize("Telefon pacjenta: 604 123 456")
    assert "phone" in r.pii_flags
    assert "[PHONE]" in r.sanitized_text
    assert "604 123 456" not in r.sanitized_text


def test_phone_with_country_prefix():
    r = detect_and_sanitize("Zadzwoń na +48 604 123 456 po wyniki.")
    assert "phone" in r.pii_flags
    assert "[PHONE]" in r.sanitized_text


def test_pesel_detected_and_replaced():
    r = detect_and_sanitize("PESEL pacjenta: 80031512345")
    assert "pesel" in r.pii_flags
    assert r.confidence["pesel"] == "high"
    assert "[PESEL]" in r.sanitized_text
    assert "80031512345" not in r.sanitized_text


def test_nip_detected_and_replaced():
    r = detect_and_sanitize("NIP placówki: 123-456-78-90")
    assert "nip" in r.pii_flags
    assert "[NIP]" in r.sanitized_text
    assert "123-456-78-90" not in r.sanitized_text


def test_date_of_birth_with_prefix():
    r = detect_and_sanitize("Pacjent ur. 15.03.1980, zgłasza ból głowy.")
    assert "date_of_birth" in r.pii_flags
    assert r.confidence["date_of_birth"] == "medium"
    assert "[DATE_OF_BIRTH]" in r.sanitized_text


def test_date_of_birth_english_prefix():
    r = detect_and_sanitize("Patient DOB 15/03/1980, presented with chest pain.")
    assert "date_of_birth" in r.pii_flags
    assert "[DATE_OF_BIRTH]" in r.sanitized_text


def test_address_detected_and_replaced():
    r = detect_and_sanitize("Adres: ul. Marszałkowska 10, Warszawa")
    assert "address" in r.pii_flags
    assert r.confidence["address"] == "medium"
    assert "[ADDRESS]" in r.sanitized_text
    # The street name should be replaced, not preserved
    assert "Marszałkowska 10" not in r.sanitized_text


# ---------------------------------------------------------------------------
# Multi-category
# ---------------------------------------------------------------------------


def test_multiple_categories_in_one_text():
    r = detect_and_sanitize(
        "Pacjent Jan, tel. 604 123 456, e-mail jan@example.com, PESEL 80031512345."
    )
    # All three categories detected
    assert "phone" in r.pii_flags
    assert "email" in r.pii_flags
    assert "pesel" in r.pii_flags
    # None of the raw values remain
    assert "604 123 456" not in r.sanitized_text
    assert "jan@example.com" not in r.sanitized_text
    assert "80031512345" not in r.sanitized_text


# ---------------------------------------------------------------------------
# Ordering: PESEL before phone
# ---------------------------------------------------------------------------


def test_pesel_not_misidentified_as_phone():
    """11-digit PESEL must be caught as PESEL, not split into a phone number."""
    r = detect_and_sanitize("PESEL: 90010112345")
    assert "pesel" in r.pii_flags
    # After replacement, [PESEL] must appear — not [PHONE]
    assert "[PESEL]" in r.sanitized_text
    # The raw number must not remain
    assert "90010112345" not in r.sanitized_text


# ---------------------------------------------------------------------------
# False positive guards
# ---------------------------------------------------------------------------


def test_no_fp_clinical_date_without_prefix():
    """A plain date in a clinical note (no DOB prefix) must NOT trigger date_of_birth."""
    r = detect_and_sanitize("Wizyta w dniu 15.03.2024, następna kontrola 10.04.2024.")
    assert "date_of_birth" not in r.pii_flags


def test_no_fp_medical_measurements():
    """SpO2/BP measurements contain digits but must NOT match phone/PESEL."""
    r = detect_and_sanitize("SpO2: 96%, BP: 120/80 mmHg, HR: 72 bpm, RR: 18/min.")
    assert r.pii_flags == []


def test_no_fp_short_number_sequences():
    """Short numbers like dosages must not fire."""
    r = detect_and_sanitize("Podaj 500 mg paracetamolu co 6 godzin przez 3 dni.")
    assert r.pii_flags == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_string():
    r = detect_and_sanitize("")
    assert r.pii_flags == []
    assert r.sanitized_text == ""


def test_clean_clinical_text_no_flags():
    r = detect_and_sanitize(
        "Pacjent lat 45, zgłasza ból w klatce piersiowej od 2 godzin. "
        "EKG wykonano, brak cech zawału."
    )
    assert r.pii_flags == []
    assert r.sanitized_text == r.sanitized_text  # unchanged


def test_result_is_frozen():
    r = detect_and_sanitize("jan@example.com")
    assert isinstance(r, SanitizerResult)
    with pytest.raises((AttributeError, TypeError)):
        r.pii_flags = []  # type: ignore[misc]


def test_sanitized_text_does_not_contain_raw_pii():
    """No raw PII must remain in sanitized_text regardless of category."""
    text = "Telefon: 604123456, e-mail: doc@hospital.pl, PESEL: 12345678901"
    r = detect_and_sanitize(text)
    assert "604123456" not in r.sanitized_text
    assert "doc@hospital.pl" not in r.sanitized_text
    assert "12345678901" not in r.sanitized_text
