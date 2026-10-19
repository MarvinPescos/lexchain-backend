"""The signed-PDF size limit is enforced while reading, not after buffering."""

import io

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from app.features.documents.workflow import intake as intake_module
from app.shared.errors.exceptions import BadRequestError
from tests.conftest import as_auth

LIMIT = 256


class CountingStream(io.BytesIO):
    """Remembers the most bytes handed out by a single read."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.largest_read = 0

    def read(self, size: int | None = -1) -> bytes:
        chunk = super().read(size)
        self.largest_read = max(self.largest_read, len(chunk))
        return chunk


def sized_pdf(total: int, *, declare_size: bool = True) -> tuple[UploadFile, CountingStream]:
    head = b"%PDF-1.7\n"
    stream = CountingStream(head + b"x" * (total - len(head)))
    upload = UploadFile(
        file=stream,
        size=total if declare_size else None,
        filename="deed.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )
    return upload, stream


@pytest.fixture(autouse=True)
def small_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake_module, "MAX_FILE_SIZE", LIMIT)


@pytest.mark.parametrize("total", [LIMIT - 1, LIMIT])
async def test_files_up_to_the_limit_upload(intake, lawyer, open_book, storage, total):
    upload, _ = sized_pdf(total)
    doc = await intake.upload_signed(as_auth(lawyer), upload, "Deed", open_book.id)

    assert doc.lifecycle == "SIGNED"
    assert [len(b) for b in storage.objects.values()] == [total]


async def test_declared_oversize_is_refused_without_reading(
    intake, lawyer, open_book, storage
):
    upload, stream = sized_pdf(LIMIT + 1)

    with pytest.raises(BadRequestError, match="maximum allowed size"):
        await intake.upload_signed(as_auth(lawyer), upload, "Deed", open_book.id)
    assert stream.largest_read == 0
    assert storage.objects == {}


async def test_undeclared_oversize_stream_is_read_only_past_the_limit(
    intake, lawyer, open_book, storage
):
    upload, stream = sized_pdf(LIMIT * 10, declare_size=False)

    with pytest.raises(BadRequestError, match="maximum allowed size"):
        await intake.upload_signed(as_auth(lawyer), upload, "Deed", open_book.id)
    assert stream.largest_read == LIMIT + 1
    assert storage.objects == {}
