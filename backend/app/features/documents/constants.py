# === Processing status ===
# Null until the document is finalized — nothing is OCR'd before that.
STATUS_QUEUED = "QUEUED"
STATUS_PROCESSING = "PROCESSING"
# OCR is done and the issuer must review the extracted text before the
# document is chunked, embedded or analyzed.
STATUS_AWAITING_REVIEW = "AWAITING_REVIEW"
STATUS_ENRICHING = "ENRICHING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"

# === Processing jobs ===
# What a ProcessingDispatch hands to the queue.
JOB_EXTRACT = "extract"  # OCR, after finalize
JOB_ENRICH = "enrich"  # chunk / embed / analyze, after the text is approved

# === Lifecycle ===
# One lifecycle, two entry points:
#   Path 1 (prepared with LexChain)  PREPARING ⇄ READY_FOR_SIGNATURE → SIGNED
#   Path 2 (already signed)          → SIGNED
# then SIGNED → FINALIZED once the lawyer confirms the signed copy.
#
# PREPARING            record exists, working draft is edited in Google Docs
# READY_FOR_SIGNATURE  text agreed; the draft goes to print
# SIGNED               signed/notarized PDF attached, waiting for the lawyer
# FINALIZED            snapshotted + anchored on-chain, immutable forever
# CANCELLED            abandoned before signing, kept for the record
LIFECYCLE_PREPARING = "PREPARING"
LIFECYCLE_READY_FOR_SIGNATURE = "READY_FOR_SIGNATURE"
LIFECYCLE_SIGNED = "SIGNED"
LIFECYCLE_FINALIZED = "FINALIZED"
LIFECYCLE_CANCELLED = "CANCELLED"

LIFECYCLES = (
    LIFECYCLE_PREPARING,
    LIFECYCLE_READY_FOR_SIGNATURE,
    LIFECYCLE_SIGNED,
    LIFECYCLE_FINALIZED,
    LIFECYCLE_CANCELLED,
)

# === Actions ===
ACTION_OPEN = "open"
ACTION_UPLOAD_SIGNED = "upload_signed"
ACTION_CREATE_DRAFT = "create_draft"
ACTION_MARK_READY = "mark_ready"
ACTION_REOPEN = "reopen"
ACTION_ATTACH_SIGNED_COPY = "attach_signed_copy"
ACTION_REPLACE_SIGNED_COPY = "replace_signed_copy"
ACTION_CORRECT_ENTRY = "correct_entry"
ACTION_CANCEL = "cancel"
ACTION_FINALIZE = "finalize"
ACTION_RENAME = "rename"
# A TAMPERED finalized document gets its archived, anchored bytes back.
# The stage is unchanged and nothing is re-anchored.
ACTION_RESTORE = "restore"

# Where a new working draft comes from.
DRAFT_SOURCE_BLANK = "blank"
DRAFT_SOURCE_TEMPLATE = "template"  # a Doc copied; the original is untouched
DRAFT_SOURCE_EXISTING = "existing"  # a Doc the lawyer already started
DRAFT_SOURCE_UPLOAD = "upload"  # a file the lawyer brings in, converted to a Doc
DRAFT_SOURCES = (
    DRAFT_SOURCE_BLANK,
    DRAFT_SOURCE_TEMPLATE,
    DRAFT_SOURCE_EXISTING,
    DRAFT_SOURCE_UPLOAD,
)

# Word-processor files Drive converts to a Google Doc: extension -> the MIME
# types browsers send for it, the first being the one Drive is told.
DRAFT_UPLOAD_TYPES = {
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
    ".doc": ("application/msword",),
    ".odt": ("application/vnd.oasis.opendocument.text",),
    ".rtf": ("application/rtf", "text/rtf", "application/x-rtf"),
}
MAX_DRAFT_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

# Actions that are not lifecycle transitions, for access checks.
ACTION_VIEW = "view"
ACTION_SHARE = "share"
ACTION_REVIEW = "review"
ACTION_RETRY_PROCESSING = "retry_processing"
ACTION_REVIEW_ECOPY = "review_ecopy"

# === E-copy requests ===
# A party asks for the unwatermarked signed PDF of a FINALIZED document; the
# owner decides. The decision and the email are tracked apart: an approved
# request whose email failed is still owed a delivery.
#
#   PENDING ──approve──> APPROVED      delivery: SENDING → SENT | FAILED
#           └─decline──> DECLINED      (FAILED → SENDING again on retry)
ECOPY_PENDING = "PENDING"
ECOPY_APPROVED = "APPROVED"
ECOPY_DECLINED = "DECLINED"
ECOPY_STATUSES = (ECOPY_PENDING, ECOPY_APPROVED, ECOPY_DECLINED)

DELIVERY_SENDING = "SENDING"
DELIVERY_SENT = "SENT"
DELIVERY_FAILED = "FAILED"

# Why a delivery failed. Codes, never provider text: the reason is shown to
# the lawyer and kept in the audit trail.
DELIVERY_ERROR_PROVIDER = "provider_error"
DELIVERY_ERROR_TOO_LARGE = "attachment_too_large"
DELIVERY_ERROR_INTEGRITY = "integrity_mismatch"
DELIVERY_ERROR_STORAGE = "storage_unavailable"
DELIVERY_ERROR_ACCESS = "access_revoked"
DELIVERY_ERROR_EMAIL_UNVERIFIED = "email_unverified"

# A send that never reported back (worker died mid-send) may be retried after
# this long, so a crash cannot leave a request stuck in SENDING forever.
DELIVERY_STALE_AFTER_SECONDS = 10 * 60

# A send still running after this is recorded as failed. Well under the stale
# window, so a hung send is given up before anyone may claim it again.
ECOPY_SEND_TIMEOUT_SECONDS = 60

# Resend refuses more than 40 MB per email after base64 (x4/3).
ECOPY_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# Audit trail actions on an e-copy request.
ECOPY_EVENT_REQUESTED = "requested"
ECOPY_EVENT_APPROVED = "approved"
ECOPY_EVENT_DECLINED = "declined"
ECOPY_EVENT_DELIVERY_ATTEMPTED = "delivery_attempted"
ECOPY_EVENT_DELIVERED = "delivered"
ECOPY_EVENT_DELIVERY_FAILED = "delivery_failed"
