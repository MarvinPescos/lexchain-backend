"""Marker on a remote PC, with MinerU taking over while it can't be reached."""

import httpx
import pytest

from app.core import settings
from app.infrastructure.nlp.ocr import ExtractionResult, build_ocr, marker_ocr
from app.infrastructure.nlp.ocr.fallback_ocr import FallbackOCRProvider
from app.infrastructure.nlp.ocr.marker_ocr import MarkerOCRProvider
from app.infrastructure.nlp.ocr.mineru_ocr import MinerUOCRProvider

REQUEST = httpx.Request("POST", "http://marker/marker/upload")


def status(code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "error", request=REQUEST, response=httpx.Response(code, request=REQUEST)
    )


class FakeEngine:
    def __init__(self, engine: str, *errors: Exception) -> None:
        self.engine = engine
        self.errors = list(errors)
        self.calls: list[bytes] = []

    async def extract(self, file_data: bytes) -> ExtractionResult:
        self.calls.append(file_data)
        if self.errors:
            raise self.errors.pop(0)
        return ExtractionResult(
            text="text", ingestion_text="text", page_count=1, meta={"engine": self.engine}
        )


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused", request=REQUEST),
        httpx.ConnectTimeout("timed out", request=REQUEST),
        httpx.ReadTimeout("timed out", request=REQUEST),
        httpx.RemoteProtocolError("dropped", request=REQUEST),
        status(502),
        status(530),
    ],
)
async def test_unreachable_marker_falls_back(error):
    marker, mineru = FakeEngine("marker-api", error), FakeEngine("mineru-api")

    result = await FallbackOCRProvider(marker, mineru, "marker").extract(b"pdf")

    assert result.meta == {"engine": "mineru-api", "fallback_from": "marker"}
    assert mineru.calls == [b"pdf"]


@pytest.mark.parametrize(
    "error", [status(400), status(422), RuntimeError("Marker failed: bad pdf")]
)
async def test_a_rejected_pdf_does_not_fall_back(error):
    marker, mineru = FakeEngine("marker-api", error), FakeEngine("mineru-api")

    with pytest.raises(type(error)):
        await FallbackOCRProvider(marker, mineru, "marker").extract(b"pdf")
    assert mineru.calls == []


async def test_reachable_marker_is_used():
    marker, mineru = FakeEngine("marker-api"), FakeEngine("mineru-api")

    result = await FallbackOCRProvider(marker, mineru, "marker").extract(b"pdf")

    assert result.meta == {"engine": "marker-api"}
    assert mineru.calls == []


async def test_after_falling_back_it_stays_on_the_fallback():
    marker = FakeEngine("marker-api", httpx.ConnectError("refused", request=REQUEST))
    mineru = FakeEngine("mineru-api")
    ocr = FallbackOCRProvider(marker, mineru, "marker")

    await ocr.extract(b"original")
    await ocr.extract(b"current")

    assert marker.calls == [b"original"]
    assert mineru.calls == [b"original", b"current"]


async def test_a_bad_gateway_from_the_real_marker_client_falls_back(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(502))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        marker_ocr.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw)
    )
    mineru = FakeEngine("mineru-api")

    result = await FallbackOCRProvider(MarkerOCRProvider(), mineru, "marker").extract(b"pdf")

    assert result.meta is not None and result.meta["fallback_from"] == "marker"


def test_factory_wraps_marker_when_a_fallback_is_set(monkeypatch):
    monkeypatch.setattr(settings, "USE_REAL_OCR", True)
    monkeypatch.setattr(settings, "OCR_ENGINE", "marker")
    monkeypatch.setattr(settings, "OCR_FALLBACK_ENGINE", "mineru")

    ocr = build_ocr(b"%PDF-1.4")

    assert isinstance(ocr, FallbackOCRProvider)
    assert isinstance(ocr.primary, MarkerOCRProvider)
    assert isinstance(ocr.fallback, MinerUOCRProvider)


@pytest.mark.parametrize("fallback", [None, "marker"])
def test_factory_without_a_different_fallback_uses_the_engine_alone(monkeypatch, fallback):
    monkeypatch.setattr(settings, "USE_REAL_OCR", True)
    monkeypatch.setattr(settings, "OCR_ENGINE", "marker")
    monkeypatch.setattr(settings, "OCR_FALLBACK_ENGINE", fallback)

    assert isinstance(build_ocr(b"%PDF-1.4"), MarkerOCRProvider)
