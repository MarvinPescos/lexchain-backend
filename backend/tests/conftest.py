"""Shared fixtures.

Tests run against `settings.TEST_DB_URL`. The schema is rebuilt from the
models once per run, and every test runs inside one outer transaction that is
rolled back at the end — services may `commit()`, which only releases a
savepoint, so nothing a test writes survives it.

External systems are faked: storage (dict-backed), the document queue
(recording), the blockchain (remembers anchors), Celery notifications, and
Google (mock sign-in + in-memory Drive).
"""

import io
import uuid
from collections.abc import AsyncGenerator

import pytest
from fastapi import UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.datastructures import Headers

import app.all_models  # noqa: F401 — registers every model on Base.metadata
from app.core import settings
from app.features.books.constants import BOOK_CLOSED, BOOK_OPEN
from app.features.books.models import Book
from app.features.blockchain.service import BlockchainService
from app.features.documents.queries import DocumentQueries
from app.features.documents.workflow.drafting import DocumentDrafting
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.documents.workflow.intake import DocumentIntake
from app.features.documents.workflow.sharing import DocumentSharing
from app.features.google.service import GoogleConnectionService
from app.features.users.models import User
from app.infrastructure.blockchain.base import ChainRecord
from app.infrastructure.database import Base
from app.infrastructure.drafts.mock_workspace import MockDraftWorkspace
from app.infrastructure.google.mock_oauth import MockGoogleOAuth
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.constants import ROLE_LAWYER, ROLE_USER
from app.shared.errors.exceptions import ConflictError

# === Database ===


@pytest.fixture(scope="session")
async def connection() -> AsyncGenerator[AsyncConnection, None]:
    engine = create_async_engine(settings.TEST_DB_URL, poolclass=NullPool)
    async with engine.connect() as conn:
        # A clean schema every run, whatever an older model left behind.
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.commit()
        yield conn
    await engine.dispose()


@pytest.fixture
async def session(connection: AsyncConnection) -> AsyncGenerator[AsyncSession, None]:
    transaction = await connection.begin()
    db = AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield db
    finally:
        await db.close()
        await transaction.rollback()


# === Fakes ===


class FakeStorage:
    """Dict-backed stand-in for SupabaseStorageClient, keyed by public URL."""

    BASE_URL = "https://storage.test/object/public/Documents/"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def upload_new(
        self, file_path: str, file_bytes: bytes, content_type: str = "application/pdf"
    ) -> str:
        url = self.BASE_URL + file_path
        if url in self.objects:
            raise ConflictError(f"Object already exists: {file_path}")
        self.objects[url] = file_bytes
        return url

    async def download(self, url: str) -> bytes:
        return self.objects[url]

    async def delete_path(self, file_path: str) -> bool:
        return self.objects.pop(self.BASE_URL + file_path, None) is not None


class RecordingQueue:
    """Remembers what would have been sent to the OCR worker."""

    def __init__(self) -> None:
        self.processed: list[uuid.UUID] = []

    async def enqueue_process_document(self, document_id, **kwargs) -> None:
        self.processed.append(document_id)

    async def enqueue_enrich_document(self, document_id) -> None:
        pass


class FakeChain:
    """Stores what it is given, so verify works without a node."""

    def __init__(self) -> None:
        self.records: dict[str, ChainRecord] = {}

    async def store_document_hash(self, document_id: str, data_hash: str) -> ChainRecord:
        record = ChainRecord(
            document_id=document_id,
            data_hash=data_hash,
            timestamp=0,
            issued_by="0x" + "00" * 20,
            tx_hash="0x" + "00" * 32,
        )
        self.records[document_id] = record
        return record

    async def get_document_hash(self, document_id: str) -> ChainRecord | None:
        return self.records.get(document_id)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    for module in ("intake", "finalization"):
        monkeypatch.setattr(
            f"app.features.documents.workflow.{module}.get_storage_client", lambda: fake
        )
    monkeypatch.setattr(
        "app.infrastructure.clients.supabase_storage.get_storage_client", lambda: fake
    )
    return fake


@pytest.fixture
def queue() -> RecordingQueue:
    return RecordingQueue()


@pytest.fixture(autouse=True)
def no_celery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Notification tasks would try to reach the broker."""
    import app.celery_tasks as tasks

    for name in ("handle_document_recorded", "handle_party_added"):
        monkeypatch.setattr(getattr(tasks, name), "delay", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def emails(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Draft-activity emails queued during the test, instead of sent."""
    import app.celery_tasks as tasks

    queued: list[dict] = []
    monkeypatch.setattr(
        tasks.send_draft_activity_email, "delay", lambda **kw: queued.append(kw)
    )
    return queued


@pytest.fixture
def chain() -> FakeChain:
    return FakeChain()


@pytest.fixture
def queries(session: AsyncSession) -> DocumentQueries:
    return DocumentQueries(session)


@pytest.fixture
def intake(session: AsyncSession, storage: FakeStorage) -> DocumentIntake:
    return DocumentIntake(session)


@pytest.fixture
def drive() -> MockDraftWorkspace:
    """A fresh in-memory Drive per test."""
    workspace = MockDraftWorkspace()
    workspace.files = {}
    return workspace


@pytest.fixture
def google(session: AsyncSession) -> GoogleConnectionService:
    oauth = MockGoogleOAuth()
    oauth.revoked = set()
    return GoogleConnectionService(session, oauth)


@pytest.fixture
def drafting(
    session: AsyncSession, drive: MockDraftWorkspace, google: GoogleConnectionService
) -> DocumentDrafting:
    return DocumentDrafting(session, drive, google)


@pytest.fixture
def sharing(session: AsyncSession, drafting: DocumentDrafting) -> DocumentSharing:
    return DocumentSharing(session, drafting)


async def connect_google(google: GoogleConnectionService, user: User) -> None:
    """Run the (mock) OAuth flow for `user`, as the callback would."""
    from urllib.parse import parse_qs, urlparse

    url = google.start(user.id).authorization_url
    query = parse_qs(urlparse(url).query)
    await google.complete(query["code"][0], query["state"][0])


@pytest.fixture
def finalization(
    session: AsyncSession, storage: FakeStorage, queue: RecordingQueue, chain: FakeChain
) -> DocumentFinalization:
    return DocumentFinalization(session, queue, chain)


@pytest.fixture
def verification(
    session: AsyncSession, storage: FakeStorage, chain: FakeChain
) -> BlockchainService:
    return BlockchainService(session, chain)


def pdf(body: str = "deed") -> UploadFile:
    """An in-memory PDF upload; a different `body` gives a different hash."""
    content = b"%PDF-1.7\n" + body.encode() + b"\n%%EOF"
    return UploadFile(
        file=io.BytesIO(content),
        filename=f"{body}.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )


# === Users and books ===


async def _make_user(session: AsyncSession, role: str, name: str) -> User:
    user = User(
        supabase_user_id=f"sb-{uuid.uuid4()}",
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        f_name=name.title(),
        l_name="Test",
        role=role,
        is_active=True,
        is_email_verified=True,
    )
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
async def lawyer(session: AsyncSession) -> User:
    return await _make_user(session, ROLE_LAWYER, "lawyer")


@pytest.fixture
async def client(session: AsyncSession) -> User:
    return await _make_user(session, ROLE_USER, "client")


@pytest.fixture
async def other_lawyer(session: AsyncSession) -> User:
    return await _make_user(session, ROLE_LAWYER, "other")


def as_auth(user: User) -> AuthenticatedUser:
    """The AuthenticatedUser a request from this user would carry."""
    return AuthenticatedUser(
        user_id=user.id, email=user.email, is_active=user.is_active, role=user.role
    )


@pytest.fixture
async def open_book(session: AsyncSession, lawyer: User) -> Book:
    book = Book(user_id=lawyer.id, book_number=1, series_year=2026, status=BOOK_OPEN)
    session.add(book)
    await session.flush()
    return book


@pytest.fixture
async def closed_book(session: AsyncSession, lawyer: User) -> Book:
    book = Book(user_id=lawyer.id, book_number=2, series_year=2025, status=BOOK_CLOSED)
    session.add(book)
    await session.flush()
    return book
