import importlib.metadata

import spacy

from .base import Chunk, ChunkResult

# Load the model once at module level — small English pipeline is enough
# for sentence segmentation. No word vectors needed.
nlp = spacy.load("en_core_web_sm")


class SpacySentenceChunker:
    """
    Sentence-aware chunker using spaCy's sentence segmenter.

    Uses a trained NLP pipeline for accurate sentence boundary detection,
    handling abbreviations, decimals, and edge cases that regex misses.
    """

    def __init__(self, max_words: int = 800, overlap: int = 1) -> None:
        self.max_words = max_words
        self.overlap = overlap  # number of sentences to carry over

    async def split(self, text: str) -> ChunkResult:
        # spaCy is synchronous, but fast for sentence segmentation
        doc = nlp(text)
        # removes empty string in the list.
        sentences = [sent.text.strip() for sent in doc.sents if sent.text.strip()]

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
            strategy="spacy_sentence_chunker",
            meta={
                "max_words": self.max_words,
                "overlap": self.overlap,
                "engine": "spacy",
                "engine_version": importlib.metadata.version("spacy"),
                "model": "en_core_web_sm",
                "chunk_count": len(chunks),
            },
        )
