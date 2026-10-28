"""Marker OCR: its JSON tree becomes the same review blocks MinerU produces."""

import json

import httpx
import pytest

from app.core import settings
from app.infrastructure.nlp.ocr import build_ocr, marker_ocr
from app.infrastructure.nlp.ocr.marker_ocr import MarkerOCRProvider
from app.infrastructure.nlp.ocr.mineru_ocr import MinerUOCRProvider

PAGE = [0, 0, 600, 800]

DOCUMENT = {
    "block_type": "Document",
    "children": [
        {
            "block_type": "Page",
            "bbox": PAGE,
            "html": "<content-ref src='/page/0/SectionHeader/0'></content-ref>",
            "children": [
                {"block_type": "PageHeader", "bbox": [0, 0, 600, 20], "html": "<p>Page 1</p>"},
                {"block_type": "SectionHeader", "bbox": [60, 40, 540, 80], "html": "<h2>DEED OF SALE</h2>"},
                {"block_type": "Text", "bbox": [60, 100, 540, 200], "html": "<p>The vendor sells the lot.</p>"},
                {
                    "block_type": "ListGroup",
                    "bbox": [60, 220, 540, 300],
                    "html": "<content-ref src='/page/0/ListItem/0'></content-ref>",
                    "children": [
                        {"block_type": "ListItem", "bbox": [60, 220, 540, 260], "html": "<li>Lot 4</li>"},
                    ],
                },
                {
                    "block_type": "Table",
                    "bbox": [60, 320, 540, 400],
                    "html": "<table><tr><td>Price</td><td>1,000,000</td></tr></table>",
                },
                {"block_type": "Picture", "bbox": [400, 600, 540, 700], "html": "<img src='seal.png'/>"},
            ],
        }
    ],
}


# Signatures and seals the OCR service took off the page before reading it.
REMOVED_REGIONS = [
    {"page": 0, "bbox_pt": [60, 560, 300, 620], "kind": "signature", "image": "removed_page_0_0.png"},
    {"page": 0, "bbox_pt": [420, 600, 540, 720], "kind": "coloured ink", "image": "removed_page_0_1.png"},
]


class FakeMarker(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        return httpx.Response(
            200,
            json={
                "format": "json",
                "output": json.dumps(DOCUMENT),
                "images": {r["image"]: "iVBORw0KGgo=" for r in REMOVED_REGIONS},
                "metadata": {"removed_regions": REMOVED_REGIONS},
                "success": True,
            },
        )


@pytest.fixture
def marker(monkeypatch: pytest.MonkeyPatch) -> FakeMarker:
    transport = FakeMarker()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        marker_ocr.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw)
    )
    return transport


async def test_marker_json_becomes_review_blocks(marker):
    result = await MarkerOCRProvider().extract(b"%PDF-1.4")

    assert marker.requests[0].url.path == "/marker/upload"
    assert b'name="output_format"\r\n\r\njson' in marker.requests[0].content

    by_type = [(b["type"], b["editable"]) for b in result.blocks]
    assert by_type == [
        ("text", False),  # page header: outlined, not approved text
        ("text", True),
        ("text", True),
        ("list", True),
        ("table", True),
        ("image", False),
        ("image", False),  # removed signature
        ("image", False),  # removed seal
    ]
    heading, table = result.blocks[1], result.blocks[4]
    assert heading["text"] == "DEED OF SALE" and heading["text_level"] == 2
    assert heading["bbox"] == [100, 50, 900, 100]  # 0–1000 like MinerU
    assert table["is_html"] and table["text"].startswith("<table>")
    assert all("html" not in b for b in result.blocks)

    assert result.page_count == 1
    assert "## DEED OF SALE" in result.ingestion_text
    assert "Page 1" not in result.ingestion_text
    assert "1,000,000" in result.ingestion_text
    assert result.meta is not None
    assert result.meta["engine"] == "marker-api"
    assert result.meta["engine_version"] == "2.0.0"


async def test_removed_signatures_and_seals_become_outlined_image_blocks(marker):
    result = await MarkerOCRProvider().extract(b"%PDF-1.4")

    signature, seal = result.blocks[-2:]
    assert signature["kind"] == "signature" and seal["kind"] == "coloured ink"
    assert signature["bbox"] == [100, 700, 500, 775]
    assert seal["bbox"] == [700, 750, 900, 900]
    assert all(b["type"] == "image" and not b["editable"] for b in (signature, seal))
    assert all(b["text"] == "" and b["page_idx"] == 0 for b in (signature, seal))
    assert [b["index"] for b in result.blocks] == list(range(len(result.blocks)))

    assert "signature" not in result.ingestion_text
    assert result.meta is not None
    assert result.meta["removed_regions"] == REMOVED_REGIONS


async def test_marker_failure_raises(monkeypatch):
    async def fail(self, request):
        return httpx.Response(200, json={"success": False, "error": "bad pdf"})

    monkeypatch.setattr(FakeMarker, "handle_async_request", fail)
    transport = FakeMarker()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        marker_ocr.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw)
    )
    with pytest.raises(RuntimeError, match="bad pdf"):
        await MarkerOCRProvider().extract(b"%PDF-1.4")


@pytest.mark.parametrize(
    "engine, provider", [("marker", MarkerOCRProvider), ("mineru", MinerUOCRProvider)]
)
def test_factory_switches_engine(monkeypatch, engine, provider):
    monkeypatch.setattr(settings, "USE_REAL_OCR", True)
    monkeypatch.setattr(settings, "OCR_ENGINE", engine)
    assert isinstance(build_ocr(b"%PDF-1.4"), provider)
