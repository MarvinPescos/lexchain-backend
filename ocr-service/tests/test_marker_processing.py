"""Marker processing adjustments, on hand-built Marker documents with Surya
faked. Skipped where Marker is not installed."""

import pytest

pytest.importorskip("marker")

from marker.converters.pdf import PdfConverter
from marker.renderers.markdown import MarkdownRenderer
from marker.schema import BlockTypes
from marker.schema.document import Document
from marker.schema.groups.page import PageGroup
from marker.schema.polygon import PolygonBox
from marker.schema.registry import get_block_class
from marker.util import strings_to_classes
from PIL import Image
from surya.recognition.schema import BlockOCRResult, PageOCRResult

from ocr_service.marker_worker import empty_blocks, processors
from ocr_service.processors import ImageOnlyReOCRProcessor

LEGAL_PAGE = [0, 0, 612, 1008]


class FakeSurya:
    """Answers every block-mode request with the next canned HTML."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, images, layout_results, full_page):
        self.requests.append((images, layout_results, full_page))
        return [
            PageOCRResult(
                blocks=[
                    BlockOCRResult(
                        polygon=layout.bboxes[0].polygon,
                        label="Text",
                        reading_order=0,
                        html=self.answers.pop(0),
                    )
                ],
                image_bbox=layout.image_bbox,
            )
            for layout in layout_results
        ]


def document(*blocks: tuple[BlockTypes, list[float], str | None]) -> Document:
    page = PageGroup(
        polygon=PolygonBox.from_bbox(LEGAL_PAGE),
        page_id=0,
        lowres_image=Image.new("RGB", (612, 1008), "white"),
        highres_image=Image.new("RGB", (1700, 2800), "white"),
        refs=[],
    )
    for block_type, bbox, html in blocks:
        block = page.add_block(get_block_class(block_type), PolygonBox.from_bbox(bbox))
        block.text_extraction_method = "surya"
        if html is not None:
            block.html = html
        page.add_structure(block)
    return Document(filepath="document.pdf", pages=[page])


def run_processors(doc: Document, processor_classes) -> str:
    converter = PdfConverter.__new__(PdfConverter)
    converter.config = {}
    converter.artifact_dict = {"recognition_model": FakeSurya(), "llm_service": None}
    converter.llm_service = None
    for processor in converter.initialize_processors(processor_classes):
        processor(doc)
    return MarkdownRenderer()(doc).markdown


def titled_deed() -> Document:
    return document(
        (BlockTypes.Text, [200, 40, 412, 60], "<p>DEED OF SALE</p>"),  # top 8%
        (BlockTypes.Text, [60, 200, 550, 400], "<p>The vendor sells the lot.</p>"),
        (BlockTypes.Text, [60, 420, 550, 600], "<p>The vendee pays the price.</p>"),
    )


def test_title_in_the_top_of_a_tall_page_is_kept():
    markdown = run_processors(titled_deed(), strings_to_classes(processors()))

    assert "DEED OF SALE" in markdown


def test_stock_processors_would_hide_that_title():
    markdown = run_processors(titled_deed(), list(PdfConverter.default_processors))

    assert "DEED OF SALE" not in markdown


def test_processor_list_swaps_marginalia_for_image_only_re_ocr():
    names = processors()

    assert not any(name.endswith(".MarginaliaProcessor") for name in names)
    assert names[0] == "ocr_service.processors.ImageOnlyReOCRProcessor"


def test_picture_with_text_becomes_a_text_block():
    doc = document((BlockTypes.Picture, [60, 800, 300, 900], None))
    surya = FakeSurya("<p>ATTY. LORENZO CRUZ</p>")

    ImageOnlyReOCRProcessor(surya)(doc)

    [(_, layouts, full_page)] = surya.requests
    assert full_page is False and layouts[0].bboxes[0].label == "Text"
    [block] = doc.pages[0].structure_blocks(doc)
    assert block.block_type == BlockTypes.Text
    assert "ATTY. LORENZO CRUZ" in block.html


def test_real_picture_and_unreadable_signature_stay_as_they_are():
    doc = document(
        (BlockTypes.Picture, [60, 800, 300, 900], None),
        (BlockTypes.Text, [60, 600, 300, 680], "<img/>"),
    )

    ImageOnlyReOCRProcessor(FakeSurya("<img/>", "<img/>"))(doc)

    picture, signature = doc.pages[0].structure_blocks(doc)
    assert picture.block_type == BlockTypes.Picture
    assert signature.html == "<img/>"


def test_empty_blocks_are_unreadable_non_picture_blocks():
    doc = document(
        (BlockTypes.Picture, [60, 800, 300, 900], "<img/>"),
        (BlockTypes.Text, [60, 600, 300, 680], "<img/>"),
        (BlockTypes.Text, [60, 200, 550, 400], "<p>PEDRO REYES</p>"),
    )

    assert empty_blocks(doc) == [(0, (60.0, 600.0, 300.0, 680.0))]
