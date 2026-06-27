import asyncio

import pytesseract
from pdf2image import convert_from_bytes

from .base import ExtractionResult


class TesseractOCRProvider:
    """Rasterize PDF pages then run Tesseract. Needs tesseract + poppler on the host"""

    def __init__(self, dpi: int = 300, lang: str = "eng") -> None:
        self.dpi = dpi
        self.lang = lang

    async def extract(self, file_data: bytes) -> ExtractionResult:
        return await asyncio.to_thread(self._extract_sync, file_data)

    def _extract_sync(self, file_data: bytes) -> ExtractionResult:
        images = convert_from_bytes(file_data, dpi=self.dpi)
        page_texts = [
            str(pytesseract.image_to_string(img, lang=self.lang)) for img in images
        ]
        text = "\n\n".join(page_texts).strip()
        return ExtractionResult(
            text=text,
            ingestion_text=text,
            page_count=len(images),
            meta={
                "engine": "tesseract",
                "engine_version": pytesseract.get_tesseract_version().__str__(),
            },
        )
