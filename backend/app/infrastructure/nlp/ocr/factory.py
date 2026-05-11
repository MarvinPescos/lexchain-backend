from pypdf import PdfReader

from app.core import settings

from .base import OCRProvider
from app.infrastructure.observability.logging_setup import log

def build_ocr(file_path: str) -> OCRProvider:
    if not settings.USE_REAL_OCR:
        from .mock_ocr import MockOCRProvider
        return MockOCRProvider()

    if settings.OCR_ENGINE == "pypdf":
        if _is_scanned_pdf(file_path):
            log.warning("scanned PDF detected but OCR_ENGINE=pypdf, falling back to tesseract")
            from .tesseract_ocr import TesseractOCRProvider
            return TesseractOCRProvider()
        from .pypdf_ocr import PyPdfOCRProvider
        return PyPdfOCRProvider()


# Pre-flight check on the PDF   
def _is_scanned_pdf(file_path: str) -> bool:
    reader = PdfReader(file_path)
    sample_pages = reader.pages[:3]  # check first 3 pages
    text = "".join(p.extract_text() or "" for p in sample_pages)
    return len(text.strip()) < 50  # threshold, tune as needed