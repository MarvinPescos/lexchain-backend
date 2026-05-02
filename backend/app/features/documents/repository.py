from app.infrastructure.database import UserScopeRepository
from sqlalchemy import select
import uuid

from .models import Document

class DocumentRepository(UserScopeRepository[Document]):
    """Repository pattern for Document"""

    def __init__(self , session):
        super().__init__(Document, session)

    # === Custom methods === 




