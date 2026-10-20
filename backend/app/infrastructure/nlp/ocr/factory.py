import io

from app.core import settings
from app.infrastructure.nlp.ocr.pypdf_ocr import PyPdfOCRProvider
from app.infrastructure.observability.logging_setup import log
from pypdf import PdfReader

from .base import OCRProvider


def build_ocr(file_data: bytes) -> OCRProvider:

    if not settings.USE_REAL_OCR:
        from .mock_ocr import MockOCRProvider

        return MockOCRProvider()

    if settings.OCR_ENGINE == "mineru":
        from .mineru_ocr import MinerUOCRProvider

        return MinerUOCRProvider()

    if settings.OCR_ENGINE == "marker":
        from .marker_ocr import MarkerOCRProvider

        return MarkerOCRProvider()

    if settings.OCR_ENGINE == "pypdf":
        if _is_scanned_pdf(file_data):
            log.warning(
                "Scanned PDF detected but OCR_ENGINE=pypdf, falling back to tesseract"
            )
            from .tesseract_ocr import TesseractOCRProvider

            return TesseractOCRProvider()

        from .pypdf_ocr import PyPdfOCRProvider

        return PyPdfOCRProvider()

    raise ValueError(f"Unsuppoerted OCR_ENGINE: {settings.OCR_ENGINE}")


# Pre-flight check on the PDF
def _is_scanned_pdf(file_data: bytes) -> bool:
    reader = PdfReader(io.BytesIO(file_data))
    sample_pages = reader.pages[:3]  # check first 3 pages
    text = "".join(p.extract_text() or "" for p in sample_pages)
    return len(text.strip()) < 50  # threshold, tune as needed
