from typing import Protocol

from pydantic import BaseModel


class ExtractionResult(BaseModel):
    text: str
    ingestion_text: str
    page_count: int
    # Positioned text blocks for the issuer review step. Engines that cannot
    # report layout (pypdf, plain tesseract) leave this empty and the review
    # falls back to whole-document text.
    blocks: list[dict] = []
    meta: dict | None = None


class OCRProvider(Protocol):
    async def extract(self, file_data: bytes) -> ExtractionResult: ...
