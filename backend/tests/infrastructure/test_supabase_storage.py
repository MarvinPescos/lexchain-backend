"""Signed PDFs live in a private bucket and are read only with the service key."""

import pytest
from storage3.exceptions import StorageApiError

from app.infrastructure.clients import supabase_storage
from app.infrastructure.clients.supabase_storage import (
    SupabaseStorageClient,
    object_path_from_url,
)


class Bucket:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def upload(self, path, file, file_options):
        self.calls.append(("upload", path))

    def download(self, path):
        self.calls.append(("download", path))
        return b"%PDF-"

    def create_signed_url(self, path, expires_in):
        self.calls.append(("sign", path, expires_in))
        return {"signedURL": f"https://storage.test/sign/{path}?token=t"}

    def get_public_url(self, path):
        raise AssertionError("no public URLs for signed PDFs")


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> Bucket:
    bucket = Bucket()

    class Storage:
        def from_(self, name):
            return bucket

    class Client:
        storage = Storage()

    monkeypatch.setattr(supabase_storage, "create_client", lambda *a: Client())
    return bucket


async def test_upload_returns_a_locator_not_a_public_url(bucket):
    storage = SupabaseStorageClient()
    url = await storage.upload_new("documents/d/signed/c.pdf", b"%PDF-")

    assert "/object/public/" not in url
    assert object_path_from_url(url) == "documents/d/signed/c.pdf"


@pytest.mark.parametrize(
    "stored",
    [
        "https://x.supabase.co/storage/v1/object/authenticated/Documents/documents/d/signed/c.pdf",
        # Written before the bucket was made private.
        "https://x.supabase.co/storage/v1/object/public/Documents/documents/d/signed/c.pdf",
    ],
)
async def test_reads_and_links_go_through_the_service_key(bucket, stored):
    storage = SupabaseStorageClient()

    assert await storage.download(stored) == b"%PDF-"
    link = await storage.signed_url(stored, 300)

    assert "token=" in link
    assert bucket.calls == [
        ("download", "documents/d/signed/c.pdf"),
        ("sign", "documents/d/signed/c.pdf", 300),
    ]


STORED = "https://x.supabase.co/storage/v1/object/authenticated/Documents/d/c.pdf"


async def test_deleted_object_reads_as_file_not_found(bucket, monkeypatch):
    def gone(path):
        raise StorageApiError("Object not found", "not_found", "404")

    monkeypatch.setattr(bucket, "download", gone)

    with pytest.raises(FileNotFoundError):
        await SupabaseStorageClient().download(STORED)


async def test_other_storage_errors_stay_outages(bucket, monkeypatch):
    def failing(path):
        raise StorageApiError("Internal error", "internal", "500")

    monkeypatch.setattr(bucket, "download", failing)

    with pytest.raises(StorageApiError):
        await SupabaseStorageClient().download(STORED)
