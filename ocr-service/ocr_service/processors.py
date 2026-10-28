"""Marker processors and block helpers. Imports Marker, so only worker
processes load this module."""

from bs4 import BeautifulSoup
from marker.processors import BaseProcessor
from marker.schema import BlockTypes
from marker.schema.document import Document
from marker.schema.registry import get_block_class
from surya.layout.schema import LayoutBox, LayoutResult
from surya.recognition import RecognitionPredictor

_IMAGE_TYPES = (BlockTypes.Picture, BlockTypes.Figure)
_TEXT_LEVEL = (BlockTypes.Line, BlockTypes.Span, BlockTypes.Char)
_TOKEN_BUDGET = 1200


def has_text(html: str) -> bool:
    return bool(BeautifulSoup(html, "html.parser").get_text(strip=True))


def leaf_blocks(document: Document, page):
    """Content blocks with no children of their own, skipping lines and spans."""
    for block in page.contained_blocks(document):
        if block.block_type in _TEXT_LEVEL or block.structure:
            continue
        yield block


def is_image_only(block) -> bool:
    html = getattr(block, "html", None)
    return html is not None and not has_text(html)


class ImageOnlyReOCRProcessor(BaseProcessor):
    """
    Ask Surya again, in block mode and labelled as Text, about every Picture or
    Figure and every block it returned as a bare image. The answer replaces the
    block only when it contains text, so real pictures stay pictures.
    """

    def __init__(self, recognition_model: RecognitionPredictor, config=None):
        super().__init__(config)
        self.recognition_model = recognition_model

    def __call__(self, document: Document):
        targets = [
            (page, block)
            for page in document.pages
            for block in leaf_blocks(document, page)
            if block.block_type in _IMAGE_TYPES or is_image_only(block)
        ]
        if not targets:
            return

        crops = [block.get_image(document, highres=True) for _, block in targets]
        layouts = [
            LayoutResult(
                bboxes=[
                    LayoutBox(
                        polygon=[[0, 0], [w, 0], [w, h], [0, h]],
                        label="Text",
                        raw_label="Text",
                        position=0,
                        count=_TOKEN_BUDGET,
                    )
                ],
                image_bbox=[0, 0, w, h],
            )
            for w, h in (crop.size for crop in crops)
        ]
        results = self.recognition_model(images=crops, layout_results=layouts, full_page=False)

        for (page, block), result in zip(targets, results, strict=True):
            answer = result.blocks[0] if result.blocks else None
            if answer is None or answer.error or answer.skipped:
                continue
            html = (answer.html or "").strip()
            if not has_text(html):
                continue
            if block.block_type in _IMAGE_TYPES:
                text = get_block_class(BlockTypes.Text)(
                    polygon=block.polygon, page_id=block.page_id, html=html
                )
                text.text_extraction_method = "surya"
                page.replace_block(block, text)
            else:
                block.html = html
