from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class BackupRef(BaseModel):
    """Where an archived object landed."""

    key: str
    size_bytes: int
    provider: str


class BackupProvider(Protocol):
    """Somewhere a finalized document is archived so it can be restored.

    Deliberately narrow — put an object, get an object back — so the destination
    can move from Supabase to an immutable off-site bucket without touching any
    caller. The interface has no delete: the whole point is that archived
    documents are not removable by the application.
    """

    async def store(self, key: str, data: bytes, content_type: str) -> BackupRef:
        """Write an object. Overwriting an existing key must not be relied upon."""
        ...

    async def fetch(self, key: str) -> bytes:
        """Read an object back for restore."""
        ...

    async def exists(self, key: str) -> bool:
        """Whether an object is present, without downloading it."""
        ...


class DumpStore(Protocol):
    """Somewhere database dumps are added. Add-only, like the archive: the
    server can't read past dumps, so there is no fetch either."""

    async def store_file(self, key: str, path: Path, content_type: str) -> None:
        """Stream a file up."""
        ...
