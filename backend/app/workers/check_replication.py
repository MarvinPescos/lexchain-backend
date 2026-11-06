"""Scheduled check that the RDS copy is still receiving changes.

Reads the replication slot on the source, so it needs nothing from the backups
account. A stopped copy loses its 5-minute recovery point silently, so problems
are emailed rather than only logged.
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings
from app.infrastructure.database import AsyncSessionLocal
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.backup.alerts import send_backup_alert

ReplicationProblem = Literal["slot_missing", "subscription_stopped", "lag_exceeded"]


class SlotState(BaseModel):
    active: bool
    lag_bytes: int | None


async def read_slot(session: AsyncSession, slot_name: str) -> SlotState | None:
    row = (
        await session.execute(
            text(
                "select active, "
                "pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) as lag "
                "from pg_replication_slots where slot_name = :slot"
            ),
            {"slot": slot_name},
        )
    ).one_or_none()
    if row is None:
        return None
    return SlotState(
        active=row.active, lag_bytes=None if row.lag is None else int(row.lag)
    )


def replication_problem(
    slot: SlotState | None, max_lag_bytes: int
) -> ReplicationProblem | None:
    if slot is None:
        return "slot_missing"
    if not slot.active:
        return "subscription_stopped"
    if slot.lag_bytes is None or slot.lag_bytes > max_lag_bytes:
        return "lag_exceeded"
    return None


async def check_replication() -> None:
    slot_name = settings.BACKUP_REPLICATION_SLOT
    if not slot_name:
        return

    async with AsyncSessionLocal() as session:
        slot = await read_slot(session, slot_name)

    problem = replication_problem(slot, settings.BACKUP_REPLICATION_MAX_LAG_BYTES)
    if problem is None:
        log.info("backup.replication.ok", lag_bytes=slot.lag_bytes if slot else None)
        return

    log.error("backup.replication.problem", problem=problem, slot=slot_name)
    await send_backup_alert(
        "database replication problem",
        f"Replication to the RDS copy needs attention: {problem} (slot {slot_name}).",
    )
