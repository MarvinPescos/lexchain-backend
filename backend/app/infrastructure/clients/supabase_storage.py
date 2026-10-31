from app.core import settings

from .supabase_client import Client, create_client


class SupabaseStorageClient:
    """Signed PDFs in a private bucket, reached only with the service role key.

    The bucket must be private: nothing here hands out a permanent URL. A
    stored `storage_url` is an internal locator for the object; readers get
    a short-lived signed URL from `signed_url`, issued after LexChain has
    checked their access.
    """

    def __init__(self, bucket_name: str = "Documents"):
        self.bucket_name = bucket_name

    def _get_client(self) -> Client:
        return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

    def locator(self, file_path: str) -> str:
        return (
            f"{settings.SUPABASE_URL}/storage/v1/object/authenticated/"
            f"{self.bucket_name}/{file_path}"
        )

    async def upload_new(
        self, file_path: str, file_bytes: bytes, content_type: str = "application/pdf"
    ) -> str:
        """Write a new object; refuses to overwrite an existing one.

        Signed copies are write-once: every upload gets its own path, so no
        upload can replace the bytes another copy points at. Returns the
        object's locator, which is not readable without the service key.
        """
        client = self._get_client()
        client.storage.from_(self.bucket_name).upload(
            path=file_path,
            file=file_bytes,
            file_options={"content-type": content_type, "x-upsert": "false"},
        )
        return self.locator(file_path)

    async def download(self, url: str) -> bytes:
        """Read a stored object from the origin with the service key.

        Authenticated reads are not served from the public CDN cache, so the
        bytes are what is stored now — integrity checks depend on that.
        """
        path = object_path_from_url(url, self.bucket_name)
        return self._get_client().storage.from_(self.bucket_name).download(path)

    async def signed_url(self, url: str, expires_in: int) -> str:
        """A URL that reads the object for `expires_in` seconds, then stops working."""
        path = object_path_from_url(url, self.bucket_name)
        signed = (
            self._get_client()
            .storage.from_(self.bucket_name)
            .create_signed_url(path, expires_in)
        )
        return signed["signedURL"]

    async def delete_path(self, file_path: str) -> bool:
        try:
            self._get_client().storage.from_(self.bucket_name).remove([file_path])
            return True
        except Exception:
            return False


def object_path_from_url(url: str, bucket: str = "Documents") -> str:
    """Recover the storage object path from a stored locator.

    Accepts the authenticated locators written now and the public URLs
    stored before the bucket was made private.

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


def get_storage_client() -> SupabaseStorageClient:
    return SupabaseStorageClient()
