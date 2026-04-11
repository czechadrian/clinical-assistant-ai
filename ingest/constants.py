"""
constants.py — shared strings and configuration values for ingest.

Import from here rather than defining local copies in main.py, validator.py,
or workflows.py.  A single definition prevents silent divergence between the
disclaimer shown in normal responses and the disclaimer injected by the repair
pass in validator.py.
"""

# ---------------------------------------------------------------------------
# Disclaimer strings — stable, safe to log
# ---------------------------------------------------------------------------

# Standard clinical disclaimer — used in triage, summary, and doc_qa responses.
DISCLAIMER_CLINICAL = (
    "Asystent AI nie zastępuje porady lekarskiej ani decyzji klinicznej. "
    "Zawsze konsultuj się z wykwalifikowanym specjalistą."
)

# Patient-facing disclaimer — used in patient_message responses.
# Must include the emergency number 112 (tested in test_run_patient_message_has_emergency_instruction).
DISCLAIMER_PATIENT = (
    "Te informacje mają charakter pomocniczy i nie zastępują indywidualnej porady lekarskiej. "
    "W nagłym przypadku zadzwoń pod numer alarmowy 112."
)

# ---------------------------------------------------------------------------
# Retrieval / UI constants
# ---------------------------------------------------------------------------

# Maximum characters for text_snippet in Source — shown in UI debug toggle, never logged.
SNIPPET_LEN = 300
