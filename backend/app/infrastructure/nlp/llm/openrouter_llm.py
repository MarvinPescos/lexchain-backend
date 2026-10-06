from typing import cast

from app.core import settings
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from .base import LLMMessage, LLMResult


class OpenRouterLLMProvider:
    """LLM access through OpenRouter.

    OpenRouter speaks the OpenAI chat-completions API, so this reuses the
    OpenAI client with a different base URL. Switching models is a settings
    change (`OPENROUTER_MODEL`) rather than a new provider class, since every
    model OpenRouter fronts is reachable over the same endpoint.
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.OPENROUTER_MODEL

        # Optional attribution headers. OpenRouter uses them to identify the
        # calling app; both are safe to omit.
        default_headers = {}
        if settings.OPENROUTER_SITE_URL:
            default_headers["HTTP-Referer"] = settings.OPENROUTER_SITE_URL
        if settings.OPENROUTER_APP_NAME:
            default_headers["X-Title"] = settings.OPENROUTER_APP_NAME

        self.client = AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
            default_headers=default_headers or None,
        )

    async def generate(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.2,
        max_tokens: int = 552,
    ) -> LLMResult:

        openrouter_messages = cast(
            list[ChatCompletionMessageParam],
            [{"role": m.role, "content": m.content} for m in messages],
        )
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=openrouter_messages,
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
                # OpenRouter reports which upstream provider actually served the
                # request, which matters when debugging quality or latency.
                "provider": getattr(response, "provider", None),
            },
        )
