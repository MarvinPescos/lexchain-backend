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

    primary = _engine(settings.OCR_ENGINE, file_data)
    fallback = settings.OCR_FALLBACK_ENGINE
    if fallback and fallback != settings.OCR_ENGINE:
        from .fallback_ocr import FallbackOCRProvider

        return FallbackOCRProvider(
            primary, _engine(fallback, file_data), settings.OCR_ENGINE
        )
    return primary


def _engine(name: str, file_data: bytes) -> OCRProvider:
    if name == "mineru":
        from .mineru_ocr import MinerUOCRProvider

        return MinerUOCRProvider()

    if name == "marker":
        from .marker_ocr import MarkerOCRProvider

        return MarkerOCRProvider()

    if name == "pypdf":
        if _is_scanned_pdf(file_data):
            log.warning(
                "Scanned PDF detected but OCR_ENGINE=pypdf, falling back to tesseract"
            )
            from .tesseract_ocr import TesseractOCRProvider

            return TesseractOCRProvider()

        from .pypdf_ocr import PyPdfOCRProvider

        return PyPdfOCRProvider()

    raise ValueError(f"Unsuppoerted OCR_ENGINE: {name}")


# Pre-flight check on the PDF
def _is_scanned_pdf(file_data: bytes) -> bool:
    reader = PdfReader(io.BytesIO(file_data))
    sample_pages = reader.pages[:3]  # check first 3 pages
    text = "".join(p.extract_text() or "" for p in sample_pages)
    return len(text.strip()) < 50  # threshold, tune as needed
