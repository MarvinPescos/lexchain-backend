from typing import Protocol

from pydantic import BaseModel

# How much of the document an analysis covered, kept in AnalysisResult.meta
# under "analysis_status". An analysis that covered nothing is not a result:
# the provider raises AnalysisFailed, so it can never read as "no risks".
ANALYSIS_STATUS_COMPLETE = "complete"
ANALYSIS_STATUS_PARTIAL = "partial"  # some sections could not be analyzed


class AnalysisFailed(Exception):
    """The model's output could not be read for any part of the document.

    Retryable: the worker retries enrichment, then marks the document FAILED.
    """


class AnalysisResult(BaseModel):
    summary: str | None = None
    labels: list[str] = []
    entities: list[dict] = []
    risk_flags: list[dict] = []
    confidence: float | None = None
    meta: dict | None = None


class AnalysisProvider(Protocol):
    async def analyze(self, text: str) -> AnalysisResult: ...
