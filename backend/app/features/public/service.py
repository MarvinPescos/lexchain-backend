import hashlib
import uuid

from datetime import datetime, timezone
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.observability.logging_setup import log
from app.infrastructure.nlp.ocr import build_ocr, OCRProvider
from app.infrastructure.nlp.embeddings import Embedder
from app.features.documents.repository import DocumentRepository
from app.features.blockchain.repository import OnChainRecordRepository
from app.features.chunks.repository import DocumentChunkRepository
from .schemas import PublicVerifyResponse

class PublicVerifierService:
    def __init__(
        self,
        session: AsyncSession,
        embedder: Embedder,
    ) -> None:
        self.session = session
        self.embedder = embedder
        self.doc_repo = DocumentRepository(session)
        self.chunk_repo = DocumentChunkRepository(session)
        self.onchain_repo = OnChainRecordRepository(session)

    async def verify(self, file: UploadFile) -> PublicVerifyResponse:
        content = await file.read()
        ocr: OCRProvider = build_ocr(content)
        result = await ocr.extract(content)
        text = result.text.strip()
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        log.info("public.verify.attempt", file_name=file.filename)
        # ── Step 1: Exact text-hash match ──
        doc = await self.doc_repo.get_by_content_hash(text_hash)
        if doc:
            record = await self.onchain_repo.get_by_document_id(doc.id)
            log.info("public.verify.match.exact", document_id=str(doc.id))
            return PublicVerifyResponse(
                status="verified",
                confidence=100.0,
                file_name=doc.file_name,
                storage_url=doc.storage_url,
                recorded_at=record.onchain_timestamp if record else None,
                recorded_by=record.issued_by if record else None,
                tx_hash=record.tx_hash if record else None,
                matched_at=datetime.now(timezone.utc),
            )
        # ── Step 2: Embedding similarity fallback ──
        embedding = await self.embedder.embed(text)
        matches = await self.chunk_repo.search_public(
            embedding.vector, top_k=3, threshold=0.75
        )
        if matches:
            chunk, score = matches[0]
            matched_doc = await self.doc_repo.get_by_id(chunk.document_id)
            record = await self.onchain_repo.get_by_document_id(chunk.document_id)
            percentage = round(score * 100, 1)
            log.info(
                "public.verify.match.fuzzy",
                document_id=str(chunk.document_id),
                score=percentage,
            )
            return PublicVerifyResponse(
                status="possible_match",
                confidence=percentage,
                file_name=matched_doc.file_name if matched_doc else None,
                storage_url=matched_doc.storage_url if percentage > 80.0 else None,
                recorded_at=record.onchain_timestamp if record else None,
                recorded_by=record.issued_by if record else None,
                tx_hash=record.tx_hash if record else None,
                matched_at=datetime.now(timezone.utc),
            )
        # ── No match ──
        log.info("public.verify.no_match")
        return PublicVerifyResponse(
            status="not_found",
            confidence=0.0,
            matched_at=datetime.now(timezone.utc),
        )