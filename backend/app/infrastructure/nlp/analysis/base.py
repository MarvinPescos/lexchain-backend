from typing import Protocol
from pydantic import BaseModel


class AnalysisResult(BaseModel):
    summary: str | None = None
    labels: list[str] = []
    entities: list[dict] = []
    risk_flags: list[dict] = []
    confidence: float | None = None
    meta: dict | None = None


class AnalysisProvider(Protocol):
    async def analyze(self, text: str) -> AnalysisResult:
        ...
