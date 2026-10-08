import re
import uuid
from pathlib import Path

import httpx
from app.core import settings
from app.infrastructure.observability.logging_setup import log
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from .base import ExtractionResult, OCRProvider


class MinerUOCRProvider(OCRProvider):
    """
    MinerU OCR Provider supporting both remote HTTP API server and local CLI execution
    """

    def __init__(self) -> None:
        self.mode = settings.MINERU_MODE.lower()
        self.api_url = settings.MINERU_API_URL.rstrip("/")
        self.backend = settings.MINERU_BACKEND
        self.timeout = settings.MINERU_TIMEOUT

    async def extract(self, file_data: bytes) -> ExtractionResult:
        if self.mode == "api":
            return await self._extract_via_api(file_data)
        else:
            raise ValueError(f"Unsupported MINERU_MODE: {self.mode}")

    async def _extract_via_api(self, file_data: bytes) -> ExtractionResult:
        """
        Sends pdf bytes to a running minerU-api server
        """

        log.info("miner.api.extract.start", url=self.api_url, backend=self.backend)

        async with httpx.AsyncClient() as client:
            # MinerU keys its results by the stem of the filename we upload, so
            # the name we send and the name we look up must be the same one.
            unique_name = f"document_{uuid.uuid4().hex}.pdf"
            files = {"files": (unique_name, file_data, "application/octet-stream")}
            data = {
                "backend": self.backend,
                "return_md": "true",
                "return_content_list": "true",
                # The OCR model's language; MinerU defaults to Chinese ("ch"),
                # which badly hurts scanned English documents.
                "lang_list": settings.MINERU_LANG,
            }

            response = await client.post(
                f"{self.api_url}/file_parse",
                files=files,
                data=data,
                timeout=self.timeout,
            )

            response.raise_for_status()
            result = response.json()

        data_res = result.get("results", {})
        stem = Path(unique_name).stem
        if stem in data_res:
            parsed = data_res[stem]
        else:
            parsed = list(data_res.values())[0] if data_res else {}

        text = parsed.get("md_content") or ""
        blocks = self._parse_blocks(parsed.get("content_list"))

        text = re.sub(r"!\[.*?\]\([^)]*\)", "", text)
        ingestion_text, render_text = self._process_tables(text)

        try:
            import io

            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(file_data))
            page_count = len(reader.pages)
        except Exception:
            page_count = 1

        return ExtractionResult(
            text=render_text.strip(),
            ingestion_text=ingestion_text,
            page_count=page_count,
            blocks=blocks,
            meta={
                "engine": f"mineru-api{self.backend}",
                "engine_version": "3.x",
                "mode": "api",
            },
        )

    @staticmethod
    def _parse_blocks(content_list) -> list[dict]:
        """Normalise MinerU's content_list into reviewable blocks.

        Each block keeps its `bbox` and `page_idx` so a review flag can point at
        a rectangle on a page instead of making the issuer read the whole thing.
        The API returns this as a JSON string, the CLI as a list.
        """
        import json

        if not content_list:
            return []

        if isinstance(content_list, str):
            try:
                content_list = json.loads(content_list)
            except json.JSONDecodeError:
                log.warning("mineru.content_list.unparseable")
                return []

        if not isinstance(content_list, list):
            return []

        blocks = []
        for item in content_list:
            if not isinstance(item, dict):
                continue

            block_type = item.get("type", "text")

            # Tables keep their content in `table_body` as HTML, not in `text`.
            # Reading only `text` would silently drop them from the review and
            # from the approved text that gets chunked and anchored — on a
            # notarial document the table is often the operative content.
            if block_type == "table":
                text = item.get("table_body") or ""
            else:
                text = item.get("text") or ""

            # Figures, seals and signatures carry no text but are still part of
            # the page. They're kept so the review UI can outline every region —
            # a notarial seal missing from the overlay looks like a gap — but
            # they are not editable and never reach the approved text.
            editable = bool(text.strip())
            if not editable and not item.get("bbox"):
                continue  # nothing to show and nothing to edit

            blocks.append(
                {
                    "index": len(blocks),
                    "editable": editable,
                    "type": block_type,
                    "text": text,
                    "original_text": text,
                    "bbox": item.get("bbox"),
                    "page_idx": item.get("page_idx", 0),
                    "text_level": item.get("text_level"),
                    "score": item.get("score"),
                    # Tables are HTML so the review UI can render rather than
                    # show markup, and the issuer edits cells not raw tags.
                    "is_html": block_type == "table",
                    "edited": False,
                }
            )
        return blocks

    @staticmethod
    def _process_tables(text: str) -> tuple[str, str]:
        ingestion_soup = BeautifulSoup(text, "html.parser")
        for table in ingestion_soup.find_all("table"):
            if table.find_parent("table"):
                continue
            table.replace_with("\n\n" + md(str(table)) + "\n\n")
        ingestion_text = str(ingestion_soup)

        render_soup = BeautifulSoup(text, "html.parser")
        for table in render_soup.find_all("table"):
            if table.find_parent("table"):
                continue
            table["style"] = "border-collapse: collapse; width 100%; margin: 1em 0;"
            for th in table.find_all("th"):
                th["style"] = (
                    "border: 1px solid #ddd; padding: 8px 12px; background-color: #f5f5f5; font-weight: bold; text-align: left;"
                )
            for td in table.find_all("td"):
                td["style"] = "border: 1px solid #ddd; padding: 8px 12px;"
        render_text = str(render_soup)

        return ingestion_text, render_text
