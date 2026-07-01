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
            # We pass a generic filename so MineU detects it as an octet stream/PDF
            unique_name = f"document_{uuid.uuid4().hex}.pdf"
            files = {"files": ("document.pdf", file_data, "application/octet-stream")}
            data = {
                "backend": self.backend,
                "return_md": "true",
                "return_content_list": "true",
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
            text = data_res[stem].get("md_content") or ""
        else:
            first_val = list(data_res.values())[0] if data_res else {}
            text = first_val.get("md_content") or ""

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
            meta={
                "engine": f"mineru-api{self.backend}",
                "engine_version": "3.x",
                "mode": "api",
            },
        )

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
