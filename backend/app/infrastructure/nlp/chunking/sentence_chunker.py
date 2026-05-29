import asyncio
import re

from .base import Chunk, ChunkResult

# NOTE: Mock sentence-aware chunker; replace with semantic/token chunkers later.


class SentenceChunker:
    """
    Simple deterministic chunker: sentence-aware with overlap (by sentences).

    Useful as a placeholder before semantic / token-aware chunkers.
    """

    def __init__(self, max_words: int = 7, overlap: int = 1) -> None:
        self.max_words = max_words
        self.overlap = overlap

    async def split(self, text: str) -> ChunkResult:
        await asyncio.sleep(1)  # simulate work

        sentences = re.split(r"(?<=[.?])\s+", text)

        current_chunk: list[str] = []
        current_word_count = 0
        chunks: list[Chunk] = []

        def flush_current() -> None:
            if not current_chunk:
                return
            chunk_text = " ".join(current_chunk).strip()
            if not chunk_text:
                return
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text=chunk_text,
                    char_count=len(chunk_text),
                    meta=None,
                )
            )

        for sentence in sentences:
            sentence_words = sentence.split()
            sentence_length = len(sentence_words)

            if current_word_count + sentence_length <= self.max_words:
                current_chunk.append(sentence)
                current_word_count += sentence_length
            else:
                overlap_part = current_chunk[-self.overlap :] if current_chunk else []
                overlap_word_count = sum(len(s.split()) for s in overlap_part)

                flush_current()

                current_chunk = overlap_part + [sentence]
                current_word_count = sentence_length + overlap_word_count

        flush_current()

        return ChunkResult(
            chunks=chunks,
            strategy="sentence_chunker",
            meta={
                "max_words": self.max_words,
                "overlap": self.overlap,
                "engine": "mock",
                "engine_version": "0.1",
                "chunk_count": len(chunks),
            },
        )
