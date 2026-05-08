import asyncio

from .base import AnalysisResult


class MockAnalysisProvider:
    async def analyze(self, text: str) -> AnalysisResult:
        await asyncio.sleep(2)  # simulate model latency
        return AnalysisResult(
            summary=f"Mock summary of {len(text)} chars.",
            labels=["NDA", "Employemnt"],
            entities=[
                {"type": "PERSON", "value": "Marvin Gwapo"},
                {"type": "ORG", "value": "Acme Corp"},
            ],
            risk_flags=[
                {"clause": "auto-renewal", "severity": "medium"}
            ],
            confidence=0.87,
            meta={"engine": "mock", "engine_version": "0.1"},
        )
