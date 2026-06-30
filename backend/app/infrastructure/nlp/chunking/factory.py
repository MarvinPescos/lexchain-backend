from app.core import settings
from app.infrastructure.observability.logging_setup import log

from .base import Chunker


def build_chunker() -> Chunker:
    if settings.CHUNKER_ENGINE == "markdown":
        from .markdown_chunker import MarkdownChunker

        return MarkdownChunker()

    from .spacy_chunker import SpacySentenceChunker

    log.warning("build.chunker", engine="Engine being used is Spacy")
    return SpacySentenceChunker()
