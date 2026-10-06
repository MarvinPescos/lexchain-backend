import uuid

from app.core import settings

from .supabase_client import Client, create_client


class SupabaseStorageClient:
    def __init__(self, bucket_name: str = "Documents"):
        self.bucket_name = bucket_name

    def _get_client(self) -> Client:
        return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

    async def upload_docs(
        self,
        file_bytes: bytes,
        user_id: uuid.UUID,
        folder: str,
        file_name: str | None = None,
        content_type: str = "pdf",
    ) -> str:

        if not file_name:
            extension = content_type.split("/")[-1]
            file_name = f"{uuid.uuid4}.{extension}"

        file_path = f"{user_id}/{folder}/{file_name}"

        client = self._get_client()
        client.storage.from_(self.bucket_name).upload(
            path=file_path,
            file=file_bytes,
            file_options={"content-type": content_type, "x-upsert": "true"},
        )

        url_response = client.storage.from_(self.bucket_name).get_public_url(file_path)

        return url_response

    async def upload_new(
        self, file_path: str, file_bytes: bytes, content_type: str = "application/pdf"
    ) -> str:
        """Write a new object; refuses to overwrite an existing one.

        Signed copies are write-once: every upload gets its own path, so no
        upload can replace the bytes another copy points at.
        """
        client = self._get_client()
        client.storage.from_(self.bucket_name).upload(
            path=file_path,
            file=file_bytes,
            file_options={"content-type": content_type, "x-upsert": "false"},
        )
        return client.storage.from_(self.bucket_name).get_public_url(file_path)

    async def download(self, url: str) -> bytes:
        """Read a stored object from the origin, never through the CDN cache."""
        return await fetch_uncached(url)

    async def delete_path(self, file_path: str) -> bool:
        try:
            self._get_client().storage.from_(self.bucket_name).remove([file_path])
            return True
        except Exception:
            return False

    async def upload_to_path(
        self, file_path: str, file_bytes: bytes, content_type: str = "application/pdf"
    ) -> str:
        """Overwrite an exact object path, rather than deriving one from a name.

        Used by restore so the document's existing object is replaced in place.
        """
        client = self._get_client()
        client.storage.from_(self.bucket_name).upload(
            path=file_path,
            file=file_bytes,
            file_options={"content-type": content_type, "x-upsert": "true"},
        )
        return client.storage.from_(self.bucket_name).get_public_url(file_path)

    async def delete_docs(
        self, folder: str, file_name: str, user_id: uuid.UUID
    ) -> bool:
        try:
            client = self._get_client()

            file_path = f"{user_id}/{folder}/{file_name}"

            # .remove expects a list
            client.storage.from_(self.bucket_name).remove([file_path])
            return True
        except Exception:
            return False


def object_path_from_url(url: str, bucket: str = "Documents") -> str:
    """Recover the storage object path from a public URL.

    Restores must overwrite the exact object a document points at. Rebuilding a
    path from `file_name` is wrong: that is a display name the owner can rename,
    while the object keeps whatever name it was uploaded under — so a rebuilt
    path silently writes a new object and leaves the tampered one in place.
    """
    from urllib.parse import unquote, urlparse

    path = unquote(urlparse(url).path)
    marker = f"/object/public/{bucket}/"
    if marker in path:
        return path.split(marker, 1)[1]
    # Signed or authenticated URL shapes keep the same tail after the bucket.
    marker = f"/{bucket}/"
    if marker in path:
        return path.split(marker, 1)[1]
    raise ValueError(f"Could not derive object path from URL: {url}")


async def fetch_uncached(url: str, timeout: int = 120) -> bytes:
    """Download a stored file bypassing any CDN cache.

    Supabase public URLs are served through a CDN with `max-age=3600`, so a
    plain GET can return a stale copy for up to an hour after the object is
    overwritten. That is unacceptable for integrity work: verification would
    compare the blockchain hash against cached bytes, and could call a restored
    document tampered — or a tampered document authentic.

    A unique query parameter puts the request outside the cache key, forcing the
    origin to answer.
    """
    import uuid as _uuid

    import httpx

    separator = "&" if "?" in url else "?"
    fresh_url = f"{url}{separator}_cb={_uuid.uuid4().hex}"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            fresh_url,
            timeout=timeout,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        response.raise_for_status()
        return response.content


def get_storage_client() -> SupabaseStorageClient:
    return SupabaseStorageClient()
