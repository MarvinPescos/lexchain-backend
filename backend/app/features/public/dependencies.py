from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.database import get_db
from app.infrastructure.nlp.embeddings import Embedder, build_embedder
from .service import PublicVerifierService

def get_embedder_public() -> Embedder:
    return build_embedder()
    
def get_public_verifier(
    db: AsyncSession = Depends(get_db),
    embedder: Embedder = Depends(get_embedder_public),
) -> PublicVerifierService:
    return PublicVerifierService(db, embedder)