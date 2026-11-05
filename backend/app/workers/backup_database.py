"""Dump the whole database, `auth` included, to the locked S3 buckets.

The server can only add dumps; restores run from the lawyer's account (see
infra/backups/RECOVERY.md). A job that stops running is caught from outside the
server, by the missing-dump alarm in infra/backups.
"""

import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from sqlalchemy.engine import URL, make_url

from app.core import settings
from app.infrastructure.observability.logging_setup import log


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


def upload(client: Any, bucket: str, key: str, path: Path) -> None:
    with path.open("rb") as body:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/gzip",
            IfNoneMatch="*",
            # Object Lock rejects a write without a checksum.
            ChecksumAlgorithm="SHA256",
        )


def upload_monthly(client: Any, bucket: str, key: str, path: Path) -> bool:
    """Keep the month's first dump. The role can't list the bucket, so the
    write-once key is what says whether this month already has one."""
    try:
        upload(client, bucket, key, path)
    except ClientError as e:
        if e.response["Error"]["Code"] == "PreconditionFailed":
            return False
        raise
    return True


def backup_database() -> None:
    if not settings.BACKUP_DB_URL or not settings.BACKUP_DB_BUCKET:
        log.info("backup.db.skipped")
        return

    now = utcnow()
    key = f"{now:%Y-%m-%dT%H-%M-%SZ}.sql.gz"
    monthly_stored = False
    try:
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "dump.sql.gz"
            dump_database(dump)
            size_bytes = dump.stat().st_size

            client = boto3.client("s3", region_name=settings.AWS_REGION)
            upload(client, settings.BACKUP_DB_BUCKET, key, dump)
            if settings.BACKUP_DB_MONTHLY_BUCKET:
                monthly_stored = upload_monthly(
                    client,
                    settings.BACKUP_DB_MONTHLY_BUCKET,
                    f"{now:%Y-%m}.sql.gz",
                    dump,
                )
    except subprocess.CalledProcessError as e:
        log.error("backup.db.failed", error=f"pg_dump exited {e.returncode}", stderr=e.stderr)
        raise
    except Exception as e:
        log.error("backup.db.failed", error=str(e))
        raise

    log.info(
        "backup.db.stored", key=key, size_bytes=size_bytes, monthly_stored=monthly_stored
    )
