from typing import Protocol
from pydantic import BaseModel

class ExtractionResult(BaseModel):
    text: str
    page_count: int
    meta: dict | None = None

class OCRProvider(Protocol):
    async def extract(self, file_data: bytes) -> ExtractionResult: ...