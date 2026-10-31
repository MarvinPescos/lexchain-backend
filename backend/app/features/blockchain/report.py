"""The integrity report: stored verification results for a lawyer's documents.

Reads only the verification history. Nothing is downloaded, re-hashed, or
read from the chain, so a report over months of checks costs one query.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.errors.exceptions import BadRequestError

from .constants import VERIFICATION_STATUSES
from .repository import VerificationResultRepository
from .schemas import (
    IntegrityReportResponse,
    IntegrityReportSummary,
    VerificationResultResponse,
)


def as_utc(value: datetime | None) -> datetime | None:
    """A time without an offset is taken to be UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class IntegrityReport:
    def __init__(self, session: AsyncSession) -> None:
        self.repo = VerificationResultRepository(session)

    async def for_owner(
        self,
        owner_id: uuid.UUID,
        verified_from: datetime | None = None,
        verified_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> IntegrityReportResponse:
        """Checks of documents `owner_id` owns, with `verified_from <= verified_at
        < verified_to`. Owner only: a party to the document is not its office."""
        verified_from, verified_to = as_utc(verified_from), as_utc(verified_to)
        if verified_from and verified_to and verified_from >= verified_to:
            raise BadRequestError("verified_from must be earlier than verified_to")

        by_status = await self.repo.count_by_status_for_owner(
            owner_id, verified_from, verified_to
        )
        rows = await self.repo.list_for_owner(
            owner_id, verified_from, verified_to, limit, offset
        )

        return IntegrityReportResponse(
            verified_from=verified_from,
            verified_to=verified_to,
            limit=limit,
            offset=offset,
            summary=IntegrityReportSummary(
                total=sum(by_status.values()),
                **{s.lower(): by_status.get(s, 0) for s in VERIFICATION_STATUSES},
            ),
            results=[
                VerificationResultResponse.model_validate(row).model_copy(
                    update={"file_name": file_name}
                )
                for row, file_name in rows
            ],
        )
