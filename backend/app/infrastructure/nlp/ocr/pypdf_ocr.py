import asyncio
from pypdf import PdfReader


from .base import ExtractionResult

class PyPdfOCRProvider:
    """Text extraction for born-digital PDFs (no image OCR)."""

    async def extract(self, file_path: str) -> ExtractionResult:
        return await asyncio.to_thread(self._extract_sync, file_path)

    def _extract_sync(self, file_path: str) -> ExtractionResult:
        reader = PdfReader(file_path)
        pages = [p.extract_text() or "" for p in reader.pages]
        text = "\n\n".join(pages).strip()
        return ExtractionResult(
            text=text,
            page_count=len(reader.pages),
            meta={
                "engine": "pypdf", "engine_version" : "real"
            },
        )
