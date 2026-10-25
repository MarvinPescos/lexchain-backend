"""The API refuses to start against a database that is not at the code's head.

Issue #25: `--reload` picked up the restore commit's model (with `from_hash` /
`to_hash`) while the database was still a revision behind, and the first signed
copy attached afterwards failed with a missing-column 500.
"""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.infrastructure.database import SchemaOutOfDateError, check_schema_revision
from app.infrastructure.database.schema import head_revision

BEFORE_RESTORE_HASHES = "d3f1a9b7c2e4"


async def _stamp(connection: AsyncConnection, revision: str) -> None:
    await connection.execute(
        text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
    )
    await connection.execute(
        text("INSERT INTO alembic_version VALUES (:rev)"), {"rev": revision}
    )


@pytest.fixture
async def conn(connection: AsyncConnection) -> AsyncGenerator[AsyncConnection, None]:
    transaction = await connection.begin()
    try:
        yield connection
    finally:
        await transaction.rollback()


async def test_passes_at_head(conn):
    await _stamp(conn, head_revision())
    await check_schema_revision(conn)


async def test_refuses_a_database_a_revision_behind(conn):
    await _stamp(conn, BEFORE_RESTORE_HASHES)
    with pytest.raises(
        SchemaOutOfDateError, match=f"{BEFORE_RESTORE_HASHES}.*{head_revision()}"
    ):
        await check_schema_revision(conn)


async def test_refuses_a_database_never_migrated(conn):
    with pytest.raises(SchemaOutOfDateError, match="alembic upgrade head"):
        await check_schema_revision(conn)


async def test_allows_a_database_ahead_of_the_code(conn):
    # Migrate first, then deploy: old code keeps serving on the newer schema.
    await _stamp(conn, "f00dfacecafe")
    await check_schema_revision(conn)
