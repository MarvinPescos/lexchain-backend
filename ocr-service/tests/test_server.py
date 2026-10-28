"""`/marker/upload` keeps the contract the backend's MarkerOCRProvider relies on."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ocr_service import marker_worker
from ocr_service.pipeline import SIGNATURE, RemovedRegion, build_response
from ocr_service.server import app

# Exactly what MarkerOCRProvider.extract sends.
BACKEND_REQUEST = {
    "files": {"file": ("document.pdf", b"%PDF-1.4", "application/pdf")},
    "data": {"output_format": "json", "force_ocr": "false"},
}


@pytest.fixture
def client():
    # No lifespan: the pool runs the (faked) worker in a thread.
    app.state.pool = ThreadPoolExecutor(max_workers=1)
    yield TestClient(app)
    app.state.pool.shutdown()


def test_backend_request_gets_output_and_removed_regions(client, monkeypatch):
    calls = []

    def convert(pdf, options):
        calls.append((pdf, options))
        region = RemovedRegion(0, [1.0, 2.0, 3.0, 4.0], SIGNATURE, Image.new("RGB", (2, 2)))
        return build_response(options["output_format"], '{"children": []}', {}, {}, [region])

    monkeypatch.setattr(marker_worker, "convert", convert)

    body = client.post("/marker/upload", **BACKEND_REQUEST).json()

    [(pdf, options)] = calls
    assert pdf == b"%PDF-1.4"
    assert options == {
        "page_range": None,
        "mode": None,
        "force_ocr": False,
        "paginate_output": False,
        "output_format": "json",
    }
    assert set(body) == {"success", "format", "output", "images", "metadata"}
    assert body["success"] is True and body["format"] == "json"
    [region] = body["metadata"]["removed_regions"]
    assert set(region) == {"page", "bbox_pt", "kind", "image"}
    assert region["image"] in body["images"]


def test_conversion_failure_returns_the_failure_shape(client, monkeypatch):
    def convert(pdf, options):
        raise ValueError("bad pdf")

    monkeypatch.setattr(marker_worker, "convert", convert)

    body = client.post("/marker/upload", **BACKEND_REQUEST).json()

    assert body == {"success": False, "error": "bad pdf"}


def test_unknown_output_format_is_a_failure(client):
    request = {**BACKEND_REQUEST, "data": {"output_format": "docx"}}

    body = client.post("/marker/upload", **request).json()

    assert body["success"] is False and "docx" in body["error"]
