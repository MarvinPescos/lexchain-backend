"""Scheduled check for use of the contract owner key.

This detects use of the key; it doesn't stop it. It runs on the server that
holds the key, so an attacker who controls that server can stop it too, though
the events stay on the public chain.
"""

from app.features.blockchain.dependencies import get_blockchain_provider
from app.features.blockchain.owner_watch import OwnerKeyWatch
from app.infrastructure.database import AsyncSessionLocal


async def watch_owner_events() -> None:
    async with AsyncSessionLocal() as session:
        await OwnerKeyWatch(session, get_blockchain_provider()).run()
