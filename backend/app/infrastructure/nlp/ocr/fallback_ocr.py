import httpx

from app.infrastructure.observability.logging_setup import log

from .base import ExtractionResult, OCRProvider


class FallbackOCRProvider(OCRProvider):
    """
    Reads with `primary`, and with `fallback` once the primary can't be reached.

    Only an unreachable engine falls back: a refused or dropped connection, a
    timeout, or a 5xx. A PDF the engine rejects still raises. Once it has fallen
    back it stays there, so files read by one provider come from one engine and
    their blocks can be compared.
    """

    def __init__(
        self, primary: OCRProvider, fallback: OCRProvider, primary_name: str
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.primary_name = primary_name
        self.fell_back = False

    async def extract(self, file_data: bytes) -> ExtractionResult:
        if not self.fell_back:
            try:
                return await self.primary.extract(file_data)
            except Exception as e:
                if not _unreachable(e):
                    raise
                log.warning(
                    "ocr.fallback",
                    engine=self.primary_name,
                    error=f"{type(e).__name__}: {e}",
                )
                self.fell_back = True

        result = await self.fallback.extract(file_data)
        return result.model_copy(
            update={"meta": {**(result.meta or {}), "fallback_from": self.primary_name}}
        )


def _unreachable(error: Exception) -> bool:
    if isinstance(error, httpx.TransportError):
        return True
    return (
        isinstance(error, httpx.HTTPStatusError)
        and error.response.status_code >= 500
    )
