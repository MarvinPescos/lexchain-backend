from typing import Iterable

from app.features.documents.models import Document
from app.infrastructure.database.base_repository import BaseRepository
from sqlalchemy import delete, select
from sqlalchemy import update as sa_update

from .models import DocumentChunk


class DocumentChunkRepository(BaseRepository[DocumentChunk]):
    def __init__(self, session) -> None:
        super().__init__(DocumentChunk, session)

    async def bulk_create(self, rows: Iterable[dict]) -> list[DocumentChunk]:
        objects = [self.model(**row) for row in rows]
        self.session.add_all(objects)
        await self.session.flush()
        return objects

    async def delete_for_document(self, document_id) -> None:
        """Drop every chunk of a document, so enrichment can rebuild them."""
        await self.session.execute(
            delete(self.model).where(self.model.document_id == document_id)
        )

    async def set_embedding(
        self, chunk_id, vector: list[float], meta: dict | None
    ) -> None:
        stmt = (
            sa_update(self.model)
            .where(self.model.id == chunk_id)
            .values(
                embedding={"vector": vector, "meta": meta},
                embedding_vec=vector,
            )
        )
        await self.session.execute(stmt)

    async def search_document(
        self,
        document_id,
        query_vector: list[float],
        top_k: int,
    ) -> list[tuple[DocumentChunk, float]]:
        # cosine distance; score = 1 - distance
        distance = self.model.embedding_vec.cosine_distance(query_vector)
        stmt = (
            select(self.model, distance.label("distance"))
            .where(self.model.document_id == document_id)
            .where(self.model.embedding_vec.is_not(None))
            .order_by(distance.asc())
            .limit(top_k)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(chunk, 1.0 - float(dist)) for chunk, dist in rows]

    async def search_user(
        self,
        user_id,
        query_vector: list[float],
        top_k: int,
    ) -> list[tuple[DocumentChunk, float]]:
        distance = self.model.embedding_vec.cosine_distance(query_vector)
        stmt = (
            select(self.model, distance.label("distance"))
            .join(Document, Document.id == self.model.document_id)
            .where(Document.user_id == user_id)
            .where(self.model.embedding_vec.is_not(None))
            .order_by(distance.asc())
            .limit(top_k)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(chunk, 1.0 - float(dist)) for chunk, dist in rows]

    async def search_public(
        self,
        query_vector: list[float],
        top_k: int = 5,
        threshold: float = 0.75,
    ) -> list[tuple[DocumentChunk, float]]:
        distance = self.model.embedding_vec.cosine_distance(query_vector)
        stmt = (
            select(self.model, distance.label("distance"))
            .where(self.model.embedding_vec.is_not(None))
            .where(distance <= (1.0 - threshold))  # cosine distance threshold
            .order_by(distance.asc())
            .limit(top_k)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(chunk, 1.0 - float(dist)) for chunk, dist in rows]
