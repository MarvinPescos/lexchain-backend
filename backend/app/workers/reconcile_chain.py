"""Scheduled check that everything anchored on-chain still exists in the database.

Report only — a missing row is restored by hand from a database dump. Findings
land in the logs as `chain.reconcile.*`, and lawyers are notified of missing rows.
"""

from app.features.blockchain.dependencies import get_blockchain_provider
from app.features.blockchain.reconciliation import ChainReconciliation
from app.infrastructure.database import AsyncSessionLocal


async def reconcile_chain() -> None:
    async with AsyncSessionLocal() as session:
        await ChainReconciliation(session, get_blockchain_provider()).run()
