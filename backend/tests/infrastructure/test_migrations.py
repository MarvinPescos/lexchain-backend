"""Migrations run for real against a throwaway database — issue #25.

The rest of the suite builds its schema with `create_all`, so it never notices
code that is ahead of the migration history. This walks the actual chain.
"""

import asyncio
import uuid
from collections.abc import Generator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core import settings
from app.infrastructure.database.schema import ALEMBIC_INI, head_revision
from tests.infrastructure.test_schema_revision import BEFORE_RESTORE_HASHES

# Sync tests on purpose: alembic's env.py calls asyncio.run itself, which cannot
# nest inside pytest-asyncio's running loop.


async def _execute_autocommit(sql: str) -> None:
    engine = create_async_engine(
        settings.TEST_DB_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    async with engine.connect() as conn:
        await conn.execute(text(sql))
    await engine.dispose()


async def _query(url: str, sql: str, params: dict[str, object]) -> list[tuple]:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        rows = (await conn.execute(text(sql), params)).all()
    await engine.dispose()
    return [tuple(r) for r in rows]


def _sql(url: str, sql: str, **params: object) -> list[tuple]:
    return asyncio.run(_query(url, sql, params))


@pytest.fixture
def scratch_db(monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    name = f"lexchain_migrations_{uuid.uuid4().hex[:8]}"
    asyncio.run(_execute_autocommit(f'CREATE DATABASE "{name}"'))
    url = make_url(settings.TEST_DB_URL).set(database=name).render_as_string(
        hide_password=False
    )
    # env.py points alembic at settings.DB_URL.
    monkeypatch.setattr(settings, "DB_URL", url)
    try:
        yield url
    finally:
        asyncio.run(_execute_autocommit(f'DROP DATABASE "{name}" WITH (FORCE)'))


def _alembic_config() -> Config:
    # No config file: env.py would otherwise run fileConfig and reset pytest's logging.
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "migrations"))
    return config


def _columns(url: str) -> set[str]:
    rows = _sql(
        url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'document_stage_changes'",
    )
    return {c for (c,) in rows}


def test_restore_hashes_upgrade_keeps_existing_stage_changes(scratch_db):
    config = _alembic_config()
    command.upgrade(config, BEFORE_RESTORE_HASHES)
    assert {"from_hash", "to_hash"}.isdisjoint(_columns(scratch_db))

    # A stage change written before the upgrade, as a live database would have.
    user_id, document_id = uuid.uuid4(), uuid.uuid4()
    _sql(
        scratch_db,
        "INSERT INTO users (id, email, supabase_user_id, f_name, l_name, role, "
        "is_active, is_email_verified) "
        "VALUES (:u, 'lawyer@migrations.test', 'sb-1', 'A', 'B', 'lawyer', true, true) "
        "RETURNING id",
        u=user_id,
    )
    _sql(
        scratch_db,
        "INSERT INTO documents (id, user_id, file_name, lifecycle, on_chain) "
        "VALUES (:d, :u, 'Deed', 'SIGNED', false) RETURNING id",
        d=document_id,
        u=user_id,
    )
    _sql(
        scratch_db,
        "INSERT INTO document_stage_changes "
        "(id, document_id, from_stage, to_stage, action, actor_id) "
        "VALUES (:i, :d, 'READY_FOR_SIGNATURE', 'SIGNED', 'attach_signed_copy', :u) "
        "RETURNING id",
        i=uuid.uuid4(),
        d=document_id,
        u=user_id,
    )

    command.upgrade(config, "head")
    assert {"from_hash", "to_hash"} <= _columns(scratch_db)
    [(version,)] = _sql(scratch_db, "SELECT version_num FROM alembic_version")
    assert version == head_revision()
    assert _sql(
        scratch_db,
        "SELECT action, from_hash, to_hash FROM document_stage_changes "
        "WHERE document_id = :d",
        d=document_id,
    ) == [("attach_signed_copy", None, None)]

    command.downgrade(config, BEFORE_RESTORE_HASHES)
    assert {"from_hash", "to_hash"}.isdisjoint(_columns(scratch_db))
