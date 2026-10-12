import asyncio

from app.core import settings
from app.infrastructure.clients.supabase_client import Client, create_client
from app.infrastructure.observability.logging_setup import log

from .base import BackupRef


class SupabaseBackupProvider:
    """Archive copies held in a separate Supabase Storage bucket.

    **This is an interim destination, not a tamper-proof archive.** It lives in
    the same Supabase project as the live files, so anyone holding the service
    key — or the console — can reach both. It protects against the *likely*
    failures (accidental deletion, a bad overwrite, corruption) and gives a
    working restore path, but not against a compromised account.

    The durable version is an object-locked bucket in a separate cloud account
    with a write-only credential. Swapping to it means implementing this same
    protocol and changing `BACKUP_PROVIDER` — no caller changes.
    """

    provider_name = "supabase"

    def __init__(self, bucket_name: str | None = None) -> None:
        self.bucket_name = bucket_name or settings.BACKUP_BUCKET

    def _client(self) -> Client:
        return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

    async def store(self, key: str, data: bytes, content_type: str) -> BackupRef:
        def _upload():
            client = self._client()
            client.storage.from_(self.bucket_name).upload(
                path=key,
                file=data,
                # Archives are written once at finalize. Upsert is enabled only so
                # a retry after a partial failure can complete rather than dying
                # on a half-written key.
                file_options={"content-type": content_type, "x-upsert": "true"},
            )

        # The supabase client is synchronous; keep it off the event loop.
        await asyncio.to_thread(_upload)

        log.info(
            "backup.stored",
            provider=self.provider_name,
            bucket=self.bucket_name,
            key=key,
            size_bytes=len(data),
        )
        return BackupRef(
            key=key, size_bytes=len(data), provider=self.provider_name
        )

    async def fetch(self, key: str) -> bytes:
        def _download() -> bytes:
            client = self._client()
            return client.storage.from_(self.bucket_name).download(key)

        data = await asyncio.to_thread(_download)
        log.info(
            "backup.fetched",
            provider=self.provider_name,
            key=key,
            size_bytes=len(data) if data else 0,
        )
        return data

    async def exists(self, key: str) -> bool:
        def _list() -> bool:
            client = self._client()
            folder, _, name = key.rpartition("/")
            entries = client.storage.from_(self.bucket_name).list(folder)
            return any(e.get("name") == name for e in entries or [])

        try:
            return await asyncio.to_thread(_list)
        except Exception as e:
            log.warning("backup.exists.failed", key=key, error=str(e))
            return False
