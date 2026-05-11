import asyncio
from .base import LLMMessage, LLMResult


class MockLLMProvider:
    async def generate(self, messages, temperature=0.2, max_tokens=512) -> LLMResult:
        await asyncio.sleep(0.5)
        user_q = next((m.content for m in messages if m.role == "user"), "")
        return LLMResult(
            answer=f"[MOCK] This is a placeholder answer to: {user_q[:80]}",
            model="mock",
            meta={"engine": "mock"},
        )