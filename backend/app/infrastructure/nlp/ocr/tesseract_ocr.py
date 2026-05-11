import asyncio

import pytesseract
from pdf2image import convert_from_path

from .base import ExtractionResult

class TesseractOCRProvider:
    """Rasterize PDF pages then run Tesseract. Needs tesseract + popplen on the host"""

    def __init__(self, dpi: int = 200, lang: str = "eng") -> None:
        self.dpi = dpi
        self.lang = lang
    
    async def extract(self, file_path: str) -> ExtractionResult:
        return await asyncio.to_thread(self._extract_sync, file_path)
    
    def _extract_sync(self, file_path: str) -> ExtractionResult:
        images = convert_from_path(file_path, dpi=self.dpi)
        page_texts = [pytesseract.image_to_string(img, lang=self.lang) for img in images]
        text = "\n\n".join(page_texts).strip()
        return ExtractionResult(
            text=text,
            page_count=len(images),
            meta={
                "engine": "tesseract", "engine_version": pytesseract.get_tesseract_version().__str__()
            }
        )

