import copy
import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm.attributes import flag_modified

from app.infrastructure.observability.logging_setup import log
from app.infrastructure.queue import DocumentQueue
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import (
    JOB_ENRICH,
    STATUS_AWAITING_REVIEW,
    STATUS_COMPLETED,
    STATUS_ENRICHING,
    LIFECYCLE_FINALIZED,
    LIFECYCLE_SIGNED,
)
from ..dispatch import DocumentDispatcher
from .blocks import blocks_to_text, count_edited
from .flagging import build_flags
from .repository import DocumentExtractionRepository
from .schemas import (
    ApproveExtractionResponse,
    BlockEdit,
    ExtractionBlock,
    ExtractionFlag,
    ExtractionReviewResponse,
)

# Statuses from which a review may still be edited/approved.
REVIEWABLE_STATUSES = (STATUS_AWAITING_REVIEW,)


class ExtractionReviewService:
    """The issuer's review of what OCR produced, before anything is derived from it."""

    def __init__(self, session: AsyncSession, queue: DocumentQueue) -> None:
        self.session = session
        self.dispatcher = DocumentDispatcher(session, queue)
        self.repo = DocumentExtractionRepository(session)
        from ..repository import DocumentRepository

        self.doc_repo = DocumentRepository(session)

    # === Read ===

    async def get_review(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> ExtractionReviewResponse:
        """Return the blocks and flags for the review screen."""
        document, extraction = await self._load(user, document_id, for_write=False)
        # An extraction exists only for a finalized, processed document.
        assert document.status is not None and document.signed_copy_id is not None

        blocks = extraction.blocks or []
        flags = extraction.flags or []

        log.info(
            "extraction.review.get",
            document_id=str(document_id),
            user_id=str(user.user_id),
            flags=len(flags),
        )

        return ExtractionReviewResponse(
            document_id=document_id,
            extraction_id=extraction.id,
            status=document.status,
            file_name=document.file_name,
            # The pages the blocks sit on are the frozen signed copy.
            signed_copy_id=document.signed_copy_id,
            engine=extraction.engine,
            page_count=extraction.page_count,
            confidence_avg=extraction.confidence_avg,
            blocks=[ExtractionBlock.model_validate(b) for b in blocks],
            flags=[ExtractionFlag.model_validate(f) for f in flags],
            flag_count=len(flags),
            high_severity_count=sum(1 for f in flags if f.get("severity") == "high"),
            edited_block_count=extraction.edited_block_count,
            is_reviewed=extraction.reviewed_at is not None,
            reviewed_by=extraction.reviewed_by,
            reviewed_at=extraction.reviewed_at,
        )

    # === Write ===

    async def run_llm_review(
        self, user: AuthenticatedUser, document_id: uuid.UUID, llm=None
    ) -> ExtractionReviewResponse:
        """Add semantic flags from the LLM on top of the deterministic ones.

        Opt-in: the free tiers already ran at extraction time, so this only costs
        inference when the issuer asks for a second opinion.
        """
        document, extraction = await self._load(user, document_id, for_write=True)

        blocks = extraction.blocks or []
        if not blocks:
            raise BadRequestError("This extraction has no reviewable blocks")

        from app.infrastructure.nlp.llm import build_llm

        from .llm_review import llm_flags

        semantic = await llm_flags(llm or build_llm(), blocks)

        # Replace any previous LLM pass; keep the deterministic flags intact.
        kept = [f for f in (extraction.flags or []) if f.get("kind") != "llm_inconsistency"]

        await self.repo.update(extraction, flags=kept + semantic)
        flag_modified(extraction, "flags")
        await self.session.commit()

        log.info(
            "extraction.review.llm",
            document_id=str(document_id),
            user_id=str(user.user_id),
            semantic_flags=len(semantic),
        )

        return await self.get_review(user, document_id)

    async def apply_edits(
        self,
        user: AuthenticatedUser,
        document_id: uuid.UUID,
        edits: list[BlockEdit],
    ) -> ExtractionReviewResponse:
        """Save block corrections. Repeatable — approval is a separate step."""
        document, extraction = await self._load(user, document_id, for_write=True)

        # Deep copy before touching anything: a shallow copy would share the dicts
        # SQLAlchemy already has loaded, so the "new" value would compare equal to
        # the old one and the JSONB column would never be written. SQLAlchemy does
        # not track in-place mutation of JSON/JSONB.
        blocks = copy.deepcopy(extraction.blocks or [])
        if not blocks:
            raise BadRequestError("This extraction has no reviewable blocks")

        by_index = {b.get("index"): b for b in blocks}
        for edit in edits:
            block = by_index.get(edit.index)
            if block is None:
                raise BadRequestError(f"No block at index {edit.index}")
            if not block.get("editable", True):
                raise BadRequestError(
                    f"Block {edit.index} is a {block.get('type', 'figure')} region "
                    "and has no editable text"
                )
            block["text"] = edit.text
            # Compare against the OCR original so reverting an edit clears the flag.
            block["edited"] = edit.text != block.get("original_text")

        # Re-flag against the corrected text so fixed problems stop being reported.
        corrected_text = blocks_to_text(blocks)
        flags = build_flags(blocks, corrected_text)

        await self.repo.update(
            extraction,
            blocks=blocks,
            flags=flags,
            edited_block_count=count_edited(blocks),
        )
        # Belt and braces for the same reason: make the JSONB writes explicit so
        # a silently skipped UPDATE can never turn the review into a no-op.
        flag_modified(extraction, "blocks")
        flag_modified(extraction, "flags")
        await self.session.commit()

        log.info(
            "extraction.review.edit",
            document_id=str(document_id),
            user_id=str(user.user_id),
            blocks_edited=len(edits),
            flags_remaining=len(flags),
        )

        return await self.get_review(user, document_id)

    async def approve(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> ApproveExtractionResponse:
        """Freeze the reviewed text and release the document to chunk/embed/analyze."""
        document, extraction = await self._load(user, document_id, for_write=True)

        blocks = extraction.blocks or []
        approved_text = blocks_to_text(blocks) if blocks else extraction.text

        if not approved_text.strip():
            raise BadRequestError("Approved text is empty")

        content_hash = hashlib.sha256(approved_text.encode("utf-8")).hexdigest()

        await self.repo.update(
            extraction,
            reviewed_text=approved_text,
            reviewed_by=user.user_id,
            reviewed_at=datetime.now(timezone.utc),
            edited_block_count=count_edited(blocks),
        )
        # content_hash describes the approved text, for search. It is not
        # anchored — the chain holds the hash of the final PDF.
        await self.doc_repo.update(document, status=STATUS_ENRICHING)
        # The enrichment job commits with the approval: if the broker is down
        # the dispatch stays pending and is resent, rather than leaving the
        # document ENRICHING with no job and no way to approve again.
        dispatch = await self.dispatcher.record(document_id, JOB_ENRICH)
        await self.session.commit()

        await self.dispatcher.send(dispatch)

        log.info(
            "extraction.review.approve",
            document_id=str(document_id),
            user_id=str(user.user_id),
            edited_blocks=extraction.edited_block_count,
            content_hash=content_hash,
        )

        return ApproveExtractionResponse(
            document_id=document_id,
            status=STATUS_ENRICHING,
            edited_block_count=extraction.edited_block_count,
            content_hash=content_hash,
            message="Text approved. Chunking, embedding and analysis have been queued.",
        )

    # === helpers ===

    async def _load(
        self, user: AuthenticatedUser, document_id: uuid.UUID, for_write: bool
    ):
        """Resolve access, then fetch the extraction.

        Uses the document access rules so review permissions cannot drift
        from the rest of the document endpoints.
        """
        from ..access import DocumentAccess
        from ..constants import ACTION_REVIEW, ACTION_VIEW

        document = await DocumentAccess(self.session).authorize(
            user, document_id, ACTION_REVIEW if for_write else ACTION_VIEW
        )

        extraction = await self.repo.get_by_document_id(document_id)
        if not extraction:
            raise NotFoundError("This document has no extraction yet")

        if for_write:
            # Locked so a replace of the signed copy cannot clear this
            # extraction while it is being edited or approved.
            await self.session.refresh(document, with_for_update=True)
            extraction = await self.repo.get_by_document_id(document_id)
            if not extraction:
                raise NotFoundError("This document has no extraction yet")
            if extraction.reviewed_at is not None:
                raise ConflictError(
                    "This extraction has already been approved and cannot be changed"
                )
            # Reviewed while SIGNED, before finalize. FINALIZED stays allowed
            # for documents finalized before processing moved to SIGNED and
            # still mid-pipeline; a new finalized document is COMPLETED, which
            # the status check below refuses.
            if document.lifecycle not in (LIFECYCLE_SIGNED, LIFECYCLE_FINALIZED):
                raise ConflictError(
                    "Extracted text can only be reviewed once a signed copy is filed"
                )
            if document.status not in REVIEWABLE_STATUSES:
                raise ConflictError(
                    f"Document is {document.status}; it can only be reviewed while "
                    f"{STATUS_AWAITING_REVIEW}"
                )

        return document, extraction
