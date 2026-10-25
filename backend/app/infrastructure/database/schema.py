from functools import cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.infrastructure.observability.logging_setup import log

from .exceptions import SchemaOutOfDateError

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


@cache
def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def head_revision() -> str:
    head = _script().get_current_head()  # raises if the history has branched
    if head is None:
        raise RuntimeError(f"No migrations under {ALEMBIC_INI.parent}")
    return head


# Added 2026-10-25 09:10 PST — refuse to serve against a database a migration
# behind the models (issue #25). The `migrate` service runs `alembic upgrade
# head` only when the stack starts, but the API runs with `--reload`: pulling a
# commit that adds a migration reloads the models without migrating. The
# restore commit added `from_hash` / `to_hash` to `document_stage_changes` that
# way, and every signed-copy attach then failed with a missing-column 500 on the
# stage-change insert. Only *behind* blocks: a revision this code does not know
# is a newer migration (migrate first, then deploy), which old code still runs on.
async def check_schema_revision(connection: AsyncConnection) -> None:
    head = head_revision()
    try:
        # Savepoint, so a missing table does not abort the caller's transaction.
        async with connection.begin_nested():
            current = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
    except ProgrammingError:
        current = None

    if current == head:
        return
    known = {rev.revision for rev in _script().walk_revisions()}
    if current is not None and current not in known:
        log.warning("database.schema.ahead", current=current, head=head)
        return
    raise SchemaOutOfDateError(
        f"Database is at revision {current or 'none'}, code expects {head}. "
        "Run `alembic upgrade head` (docker compose run --rm migrate) and restart."
    )
