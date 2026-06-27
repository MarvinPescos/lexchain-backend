import asyncio
import os
import tempfile
import json
from pathlib import Path
import httpx

from app.core import settings
from app.infrastructure.observability.logging_setup import log
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

            files = {"files" : ("document.pdf", file_data, "application/octet-stream")}
            data = {
                "backend" : self.backend,
                "return_md" : "true",
                "return_content_list": "true",
            }

            response = await client.post(
                f"{self.api_url}/file_parse",
                files=files,
                data=data,
                timeout=self.timeout
            )

            response.raise_for_status()
            result = response.json()

        data_res = result.get("results", result)
        text = data_res.get("md_content") or data_res.get("markdown") or ""
        page_count = data_res.get("page_count") or data_res.get("total_pages") or 1
        
        return ExtractionResult(
            text=text.strip(),
            page_count=page_count,
            meta={
                "engine": f"mineru-api{self.backend}",
                "engine_version": "3.x",
                "mode": "api"
            }
        )

