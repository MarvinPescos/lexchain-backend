from typing import Protocol
from pydantic import BaseModel

class LLMMessage(BaseModel):
    role: str
    content: str

class LLMResult(BaseModel):
    answer: str
    model: str
    meta: dict | None = None

class LLMProvider(Protocol):
    async def generate(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.2,
        max_tokens: int = 512,
    ) -> LLMResult:
        ...
