import json
import re

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from app.core import settings
from app.infrastructure.observability.logging_setup import log

from .base import ExtractionResult, OCRProvider

# Marker block types mapped onto the block types the review screen already
# knows from MinerU. Anything unlisted is treated as text.
_BLOCK_TYPES = {
    "Table": "table",
    "TableOfContents": "table",
    "Form": "table",
    "Picture": "image",
    "Figure": "image",
    "Equation": "equation",
    "ListItem": "list",
}

# Regions shown on the page overlay but kept out of the approved text: they
# carry no content of their own (images) or repeat on every page.
_NON_EDITABLE = {"Picture", "Figure", "PageHeader", "PageFooter"}


class MarkerOCRProvider(OCRProvider):
    """
    Marker OCR provider, calling a running `marker_server` over HTTP.

    Asks for Marker's JSON output, whose leaf blocks carry both their HTML and
    a bbox, so one call gives the review blocks and the document text.
    """

    def __init__(self) -> None:
        self.api_url = settings.MARKER_API_URL.rstrip("/")
        self.force_ocr = settings.MARKER_FORCE_OCR
        self.timeout = settings.MARKER_TIMEOUT

    async def extract(self, file_data: bytes) -> ExtractionResult:
        log.info("marker.api.extract.start", url=self.api_url, force_ocr=self.force_ocr)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.api_url}/marker/upload",
                files={"file": ("document.pdf", file_data, "application/pdf")},
                data={
                    "output_format": "json",
                    "force_ocr": str(self.force_ocr).lower(),
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()

        if not result.get("success", True):
            raise RuntimeError(f"Marker failed: {result.get('error')}")

        output = result.get("output") or "{}"
        document = json.loads(output) if isinstance(output, str) else output
        pages = [p for p in document.get("children") or [] if isinstance(p, dict)]

        blocks = self._parse_blocks(pages)
        html = "\n\n".join(b["html"] for b in blocks if b["editable"])
        for b in blocks:
            del b["html"]

        ingestion_text = md(html, heading_style="ATX").strip()
        ingestion_text = re.sub(r"\n{3,}", "\n\n", ingestion_text)

        return ExtractionResult(
            text=ingestion_text,
            ingestion_text=ingestion_text,
            page_count=len(pages) or 1,
            blocks=blocks,
            meta={
                "engine": "marker-api",
                "engine_version": "1.x",
                "mode": "api",
                "force_ocr": self.force_ocr,
            },
        )

    @classmethod
    def _parse_blocks(cls, pages: list[dict]) -> list[dict]:
        """Flatten Marker's page tree into reviewable blocks, in reading order."""
        blocks: list[dict] = []
        for page_idx, page in enumerate(pages):
            page_box = page.get("bbox") or [0, 0, 0, 0]
            for leaf in cls._leaves(page):
                marker_type = leaf.get("block_type", "Text")
                block_html = leaf.get("html") or ""
                is_table = _BLOCK_TYPES.get(marker_type) == "table"

                if is_table:
                    text = cls._table_html(block_html)
                else:
                    text = BeautifulSoup(block_html, "html.parser").get_text(" ").strip()
                    text = re.sub(r"[ \t]+", " ", text)

                editable = bool(text.strip()) and marker_type not in _NON_EDITABLE
                bbox = cls._normalize(leaf.get("bbox"), page_box)
                if not editable and bbox is None:
                    continue

                blocks.append(
                    {
                        "index": len(blocks),
                        "editable": editable,
                        "type": _BLOCK_TYPES.get(marker_type, "text"),
                        "text": text if editable else "",
                        "original_text": text if editable else "",
                        "bbox": bbox,
                        "page_idx": page_idx,
                        "text_level": cls._heading_level(marker_type, block_html),
                        "score": None,
                        "is_html": is_table,
                        "edited": False,
                        "html": block_html,
                    }
                )
        return blocks

    @classmethod
    def _leaves(cls, block: dict):
        """Group blocks (pages, list/table/figure groups) hold content-refs; the
        leaves hold the rendered HTML."""
        for child in block.get("children") or []:
            if child.get("children"):
                yield from cls._leaves(child)
            else:
                yield child

    @staticmethod
    def _normalize(bbox, page_box) -> list[int] | None:
        """Scale to 0–1000 per page, the same frame MinerU reports, so the
        review overlay does not care which engine ran."""
        if not bbox or len(bbox) != 4:
            return None
        x0, y0, x1, y1 = page_box
        width, height = x1 - x0, y1 - y0
        if width <= 0 or height <= 0:
            return None
        return [
            round((bbox[0] - x0) / width * 1000),
            round((bbox[1] - y0) / height * 1000),
            round((bbox[2] - x0) / width * 1000),
            round((bbox[3] - y0) / height * 1000),
        ]

    @staticmethod
    def _heading_level(marker_type: str, block_html: str) -> int | None:
        if marker_type != "SectionHeader":
            return None
        match = re.match(r"\s*<h([1-6])", block_html)
        return int(match.group(1)) if match else 1

    @staticmethod
    def _table_html(block_html: str) -> str:
        """Keep only the <table>, as MinerU's table_body does."""
        table = BeautifulSoup(block_html, "html.parser").find("table")
        return str(table) if table else ""
