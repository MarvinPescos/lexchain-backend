from typing import cast

from app.core import settings
from groq import AsyncGroq
from groq.types.chat import ChatCompletionMessageParam

from .base import LLMMessage, LLMResult


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

        groq_messages = cast(
            list[ChatCompletionMessageParam],
            [{"role": m.role, "content": m.content} for m in messages],
        )
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=groq_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        usage = response.usage
        return LLMResult(
            answer=response.choices[0].message.content or "",
            model=self.model,
            meta={
                "finish_reason": response.choices[0].finish_reason,
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
            },
        )
