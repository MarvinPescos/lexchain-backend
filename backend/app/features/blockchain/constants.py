"""Verdicts of a verification, as returned and as kept in its history.

VERIFICATION_UNAVAILABLE is its own verdict: integrity unknown, never read
as authentic or as tampered. FILE_MISSING is a tamper finding: the anchored file,
or the record pointing at it, is gone.
"""

VERIFY_AUTHENTIC = "AUTHENTIC"
VERIFY_TAMPERED = "TAMPERED"
VERIFY_FILE_MISSING = "FILE_MISSING"
VERIFY_SNAPSHOT_COMPROMISED = "SNAPSHOT_COMPROMISED"
VERIFY_NOT_ANCHORED = "NOT_ANCHORED"
VERIFY_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"
# A finding about the database records, kept only in the history: the verdict
# of a verification is always about the PDF.
VERIFY_RECORD_ALTERED = "RECORD_ALTERED"

# Verdicts that say the live file is not the anchored one.
VERIFY_TAMPER_FINDINGS = (
    VERIFY_TAMPERED,
    VERIFY_FILE_MISSING,
    VERIFY_SNAPSHOT_COMPROMISED,
)

VERIFICATION_STATUSES = (
    VERIFY_AUTHENTIC,
    VERIFY_TAMPERED,
    VERIFY_FILE_MISSING,
    VERIFY_SNAPSHOT_COMPROMISED,
    VERIFY_NOT_ANCHORED,
    VERIFY_UNAVAILABLE,
    VERIFY_RECORD_ALTERED,
)
