"""The pipeline: clean, convert, erase signatures, convert again only if needed."""

import base64
import io

import numpy as np
from PIL import Image

from ocr_service.pipeline import (
    COLOURED_INK,
    DPI,
    SIGNATURE,
    MarkerPass,
    RemovedRegion,
    build_response,
    run,
)

PX_PER_PT = DPI / 72


def blank_page() -> np.ndarray:
    return np.full((800, 600, 3), 255, dtype=np.uint8)


def signed_page() -> np.ndarray:
    img = blank_page()
    img[300:310, 200:400] = (40, 40, 40)  # handwriting
    img[330:332, 150:450] = 0  # signature line
    for x in range(200, 400, 12):  # printed name, letter by letter
        img[350:360, x : x + 7] = 0
    return img


# The signature block as Marker reports it, in points.
SIGNATURE_BLOCK_PT = (180 / PX_PER_PT, 290 / PX_PER_PT, 420 / PX_PER_PT, 365 / PX_PER_PT)


class FakeMarker:
    def __init__(self, empty_blocks):
        self.empty_blocks = empty_blocks
        self.pdfs: list[bytes] = []

    def __call__(self, pdf: bytes) -> MarkerPass:
        self.pdfs.append(pdf)
        return MarkerPass(rendered=f"pass {len(self.pdfs)}", empty_blocks=self.empty_blocks)


def pdf_pages(pdf: bytes) -> list[np.ndarray]:
    import pypdfium2

    doc = pypdfium2.PdfDocument(pdf)
    return [np.asarray(p.render(scale=DPI / 72).to_pil().convert("RGB")) for p in doc]


def test_document_without_signatures_costs_one_pass():
    marker = FakeMarker(empty_blocks=[])

    result = run([Image.fromarray(blank_page())], marker)

    assert len(marker.pdfs) == 1
    assert result.rendered == "pass 1"
    assert result.removed == []


def test_empty_block_without_a_line_costs_one_pass():
    marker = FakeMarker(empty_blocks=[(0, (10, 10, 100, 60))])

    result = run([Image.fromarray(blank_page())], marker)

    assert len(marker.pdfs) == 1
    assert result.removed == []


def test_erased_signature_triggers_a_second_pass_on_the_erased_page():
    original = signed_page()
    marker = FakeMarker(empty_blocks=[(0, SIGNATURE_BLOCK_PT)])

    result = run([Image.fromarray(original)], marker)

    assert len(marker.pdfs) == 2
    assert result.rendered == "pass 2"
    second = pdf_pages(marker.pdfs[1])[0]
    assert second[300:332, 200:400].min() > 200  # handwriting and line gone
    assert second[350:360, 200:207].max() < 60  # printed name kept

    [region] = result.removed
    assert region.kind == SIGNATURE and region.page == 0
    x0, y0, x1, y1 = region.bbox_pt
    # padded 15 pt left/right and 3 pt up, down to the line
    assert round(x0) == round(SIGNATURE_BLOCK_PT[0] - 15)
    assert round(x1) == round(SIGNATURE_BLOCK_PT[2] + 15)
    assert round(y0) == round(SIGNATURE_BLOCK_PT[1] - 3)
    assert round(y1 * PX_PER_PT) == 332
    # the crop holds the original pixels, handwriting included
    crop = np.asarray(region.image)
    assert crop.shape[0] == round((y1 - y0) * PX_PER_PT) and crop.min() < 60


def test_coloured_ink_is_removed_before_the_first_pass_and_kept():
    original = blank_page()
    original[100:140, 100:260] = (30, 60, 200)
    marker = FakeMarker(empty_blocks=[])

    result = run([Image.fromarray(original)], marker, page_numbers=[4])

    first = pdf_pages(marker.pdfs[0])[0]
    assert first[100:140, 100:260].min() > 200
    [region] = result.removed
    assert region.kind == COLOURED_INK and region.page == 4
    assert [round(v * PX_PER_PT) for v in region.bbox_pt] == [97, 97, 263, 143]
    assert np.asarray(region.image)[20, 20].tolist() == [30, 60, 200]


def regions() -> list[RemovedRegion]:
    return [
        RemovedRegion(0, [10.0, 20.0, 30.0, 40.0], SIGNATURE, Image.new("RGB", (4, 4))),
        RemovedRegion(1, [50.0, 60.0, 70.0, 80.0], COLOURED_INK, Image.new("RGB", (2, 2))),
    ]


def test_response_lists_removed_regions_with_png_images():
    response = build_response("json", '{"children": []}', {}, {"page_stats": []}, regions())

    assert response["success"] is True
    assert response["format"] == "json"
    assert response["output"] == '{"children": []}'
    removed = response["metadata"]["removed_regions"]
    assert response["metadata"]["page_stats"] == []
    assert [(r["page"], r["kind"], r["bbox_pt"]) for r in removed] == [
        (0, SIGNATURE, [10.0, 20.0, 30.0, 40.0]),
        (1, COLOURED_INK, [50.0, 60.0, 70.0, 80.0]),
    ]
    for r in removed:
        png = base64.b64decode(response["images"][r["image"]])
        assert Image.open(io.BytesIO(png)).format == "PNG"


def test_markdown_and_html_get_a_signatures_and_seals_section():
    markdown = build_response("markdown", "# Deed", {}, {}, regions())["output"]
    html = build_response("html", "<h1>Deed</h1>", {}, {}, regions())["output"]

    assert markdown.startswith("# Deed")
    assert "## Signatures and seals" in markdown
    assert markdown.count("![") == 2
    assert "<h2>Signatures and seals</h2>" in html
    assert html.count("<img") == 2

    json_output = build_response("json", "{}", {}, {}, regions())["output"]
    assert json_output == "{}"


def test_document_without_regions_gets_no_section():
    response = build_response("markdown", "# Deed", {}, {}, [])

    assert response["output"] == "# Deed"
    assert response["metadata"]["removed_regions"] == []
