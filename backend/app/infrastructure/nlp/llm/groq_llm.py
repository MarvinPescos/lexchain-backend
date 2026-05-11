from groq import AsyncGroq

from .base import LLMMessage, LLMResult
from app.core import settings


class GroqLLMProvider:
    def __init__(self, model: str = "llama-3.1-8b-instant") -> None:
        self.model = model
        self.client = AsyncGroq(api_key=settings.GROQ_API_KEY)

    async def generate(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.2,
        max_tokens: int = 552,
    ) -> LLMResult:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": m.role, "content": m.content}for m in messages],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return LLMResult(
            answer=response.choices[0].message.content or "",
            model=self.model,
            meta={
                "finish_reason": response.choices[0].finish_reason,
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            }
        )