from typing import cast

from app.core import settings
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from .base import LLMMessage, LLMResult


class NvidiaLLMProvider:
    def __init__(self, model: str = "meta/llama-3.1-70b-instruct") -> None:
        self.model = model
        self.client = AsyncOpenAI(
            api_key=settings.NVIDIA_API_KEY,
            base_url="https://integrate.api.nvidia.com/v1",
        )

    async def generate(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.2,
        max_tokens: int = 552,
    ) -> LLMResult:

        nvidia_messages = cast(
            list[ChatCompletionMessageParam],
            [{"role": m.role, "content": m.content} for m in messages],
        )
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=nvidia_messages,
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
