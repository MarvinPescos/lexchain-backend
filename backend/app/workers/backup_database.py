"""Dump the whole database, `auth` included, to the locked S3 buckets.

The server can only add dumps; restores run from the lawyer's account (see
infra/backups/RECOVERY.md). A job that stops running is caught from outside the
server, by the missing-dump alarm in infra/backups.
"""

import asyncio
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import URL, make_url

from app.core import settings
from app.infrastructure.backup import build_dump_store
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.backup.alerts import send_backup_alert

DUMP_CONTENT_TYPE = "application/gzip"
DUMP_FAILED = "database backup failed"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dump_database(target: Path) -> None:
    """Write a gzipped plain SQL dump, streamed to disk by pg_dump itself."""
    assert settings.BACKUP_DB_URL is not None
    url = make_url(settings.BACKUP_DB_URL)
    # The password goes through the environment: arguments are visible in `ps`.
    conninfo = URL.create(
        url.drivername,
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    ).render_as_string(hide_password=False)
    subprocess.run(
        ["pg_dump", "--compress=6", f"--file={target}", f"--dbname={conninfo}"],
        env={**os.environ, "PGPASSWORD": str(url.password or "")},
        check=True,
        capture_output=True,
        text=True,
        timeout=settings.BACKUP_DB_TIMEOUT_SECONDS,
    )


async def backup_database() -> None:
    if not settings.BACKUP_DB_URL or not settings.BACKUP_DB_BUCKET:
        log.info("backup.db.skipped")
        return

    now = utcnow()
    key = f"{now:%Y-%m-%dT%H-%M-%SZ}.sql.gz"
    # Every dump of the month's first day, not just the first one: the role
    # can't list the bucket, and a fixed monthly key could be claimed in advance
    # by a compromised server, silently blocking the real dump.
    monthly = settings.BACKUP_DB_MONTHLY_BUCKET if now.day == 1 else None
    try:
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "dump.sql.gz"
            await asyncio.to_thread(dump_database, dump)
            size_bytes = dump.stat().st_size

            await build_dump_store(settings.BACKUP_DB_BUCKET).store_file(
                key, dump, DUMP_CONTENT_TYPE
            )
            if monthly:
                await build_dump_store(monthly).store_file(
                    key, dump, DUMP_CONTENT_TYPE
                )
    except subprocess.CalledProcessError as e:
        log.error(
            "backup.db.failed", error=f"pg_dump exited {e.returncode}", stderr=e.stderr
        )
        await send_backup_alert(DUMP_FAILED, f"pg_dump exited {e.returncode}.")
        raise
    except Exception as e:
        log.error("backup.db.failed", error=str(e))
        await send_backup_alert(DUMP_FAILED, f"The dump {key} was not stored.")
        raise

    log.info(
        "backup.db.stored", key=key, size_bytes=size_bytes, monthly=monthly is not None
    )
