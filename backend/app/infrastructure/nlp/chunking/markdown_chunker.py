import re

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from .base import Chunk, ChunkResult


class MarkdownChunker:
    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        header_to_split_on: list[tuple[str, str]] | None = None,
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.header_to_split_on = header_to_split_on or [
            ("#", "h1"),
            ("##", "h2"),
            ("###", "h3"),
        ]

    async def split(self, text: str) -> ChunkResult:
        header_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=self.header_to_split_on, strip_headers=False
        )

        char_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap
        )

        sections = header_splitter.split_text(text)
        chunks: list[Chunk] = []

        for section in sections:
            content = section.page_content.strip()
            if not content:
                continue

            if self._has_table(content):
                chunks.append(
                    Chunk(
                        index=len(chunks),
                        text=content,
                        char_count=len(content),
                        meta=section.metadata,
                    )
                )
            else:
                for piece in char_splitter.split_text(content):
                    piece = piece.strip()
                    if not piece:
                        continue
                    chunks.append(
                        Chunk(
                            index=len(chunks),
                            text=piece,
                            char_count=len(piece),
                            meta=section.metadata,
                        )
                    )

        return ChunkResult(
            chunks=chunks,
            strategy="markdown_chunker",
            meta={
                "chunk_size": self.chunk_size,
                "chunk_overlap": self.chunk_overlap,
                "chunk_count": len(chunks),
                "engine": "langchain",
            },
        )

    @staticmethod
    def _has_table(text: str) -> bool:
        return bool(re.search(r"^\|", text, re.MULTILINE))
