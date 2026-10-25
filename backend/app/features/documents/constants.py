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
DRAFT_SOURCES = (DRAFT_SOURCE_BLANK, DRAFT_SOURCE_TEMPLATE, DRAFT_SOURCE_EXISTING)

# Actions that are not lifecycle transitions, for access checks.
ACTION_VIEW = "view"
ACTION_SHARE = "share"
ACTION_REVIEW = "review"
ACTION_RETRY_PROCESSING = "retry_processing"
