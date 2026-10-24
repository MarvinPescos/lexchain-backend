"""Every lifecycle rule for a document, in one place.

Pure: no I/O. `transition` is the only code that assigns `Document.lifecycle`;
it returns the audit row for the caller to add. Nothing here performs a stage
on the lawyer's behalf — an action from the wrong stage is refused, not bridged.
"""

import uuid

from app.shared.errors.exceptions import ConflictError

from .constants import (
    ACTION_ATTACH_SIGNED_COPY,
    ACTION_CANCEL,
    ACTION_CORRECT_ENTRY,
    ACTION_FINALIZE,
    ACTION_CREATE_DRAFT,
    ACTION_MARK_READY,
    ACTION_OPEN,
    ACTION_RENAME,
    ACTION_REOPEN,
    ACTION_REPLACE_SIGNED_COPY,
    ACTION_RESTORE,
    ACTION_UPLOAD_SIGNED,
    LIFECYCLE_CANCELLED,
    LIFECYCLE_FINALIZED,
    LIFECYCLE_PREPARING,
    LIFECYCLE_READY_FOR_SIGNATURE,
    LIFECYCLE_SIGNED,
)
from .models import Document, DocumentStageChange

# action -> (stages it may start from, stage it ends in; None = unchanged).
# A source of None means the action creates the document.
TRANSITIONS: dict[str, tuple[tuple[str | None, ...], str | None]] = {
    ACTION_OPEN: ((None,), LIFECYCLE_PREPARING),
    ACTION_UPLOAD_SIGNED: ((None,), LIFECYCLE_SIGNED),
    ACTION_CREATE_DRAFT: ((LIFECYCLE_PREPARING,), None),
    ACTION_MARK_READY: ((LIFECYCLE_PREPARING,), LIFECYCLE_READY_FOR_SIGNATURE),
    ACTION_REOPEN: ((LIFECYCLE_READY_FOR_SIGNATURE,), LIFECYCLE_PREPARING),
    ACTION_ATTACH_SIGNED_COPY: ((LIFECYCLE_READY_FOR_SIGNATURE,), LIFECYCLE_SIGNED),
    ACTION_REPLACE_SIGNED_COPY: ((LIFECYCLE_SIGNED,), None),
    ACTION_CORRECT_ENTRY: ((LIFECYCLE_SIGNED,), None),
    ACTION_CANCEL: (
        (LIFECYCLE_PREPARING, LIFECYCLE_READY_FOR_SIGNATURE),
        LIFECYCLE_CANCELLED,
    ),
    ACTION_FINALIZE: ((LIFECYCLE_SIGNED,), LIFECYCLE_FINALIZED),
    ACTION_RENAME: (
        (LIFECYCLE_PREPARING, LIFECYCLE_READY_FOR_SIGNATURE, LIFECYCLE_SIGNED),
        None,
    ),
    ACTION_RESTORE: ((LIFECYCLE_FINALIZED,), None),
}

# What to tell the lawyer when an action is refused from a given stage.
_HINTS: dict[tuple[str, str], str] = {
    (ACTION_ATTACH_SIGNED_COPY, LIFECYCLE_PREPARING): (
        "Mark the document Ready for signature before attaching the signed copy."
    ),
    (ACTION_FINALIZE, LIFECYCLE_PREPARING): (
        "Only a signed document can be finalized. Mark it Ready for signature, "
        "then attach the signed copy."
    ),
    (ACTION_FINALIZE, LIFECYCLE_READY_FOR_SIGNATURE): (
        "Only a signed document can be finalized. Attach the signed copy first."
    ),
    (ACTION_CREATE_DRAFT, LIFECYCLE_READY_FOR_SIGNATURE): (
        "The text is already agreed. Reopen the document to change its draft."
    ),
}


def can(action: str, document: Document | None) -> bool:
    """Whether `action` is allowed from the document's current stage."""
    sources, _ = TRANSITIONS[action]
    stage = document.lifecycle if document is not None else None
    return stage in sources


def ensure(action: str, document: Document | None) -> None:
    """Raise ConflictError unless `action` is allowed now."""
    if can(action, document):
        return
    stage = document.lifecycle if document is not None else ""
    hint = _HINTS.get((action, stage))
    label = stage.replace("_", " ").lower()
    raise ConflictError(
        hint or f"Cannot {action.replace('_', ' ')} a document that is {label}."
    )


def transition(
    action: str, document: Document, actor_id: uuid.UUID, *, created: bool = False
) -> DocumentStageChange:
    """Apply `action` to the document's stage and return the audit row.

    `created=True` for the two actions that create the document; the row's
    from_stage is then null.
    """
    from_stage = None if created else document.lifecycle
    if not created:
        ensure(action, document)
    _, to_stage = TRANSITIONS[action]
    if to_stage is not None:
        document.lifecycle = to_stage
    return DocumentStageChange(
        document_id=document.id,
        from_stage=from_stage,
        to_stage=document.lifecycle,
        action=action,
        actor_id=actor_id,
    )
