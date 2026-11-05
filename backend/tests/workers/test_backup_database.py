import subprocess
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from app.celery_app import celery_app
from app.core import settings
from app.infrastructure.backup.s3_backup import S3BackupProvider
from app.workers import backup_database as job

NOW = datetime(2026, 11, 5, 3, 0, 7, tzinfo=timezone.utc)
KEY = "2026-11-05T03-00-07Z.sql.gz"
FIRST_OF_MONTH = datetime(2026, 11, 1, 3, 0, 7, tzinfo=timezone.utc)
FIRST_OF_MONTH_KEY = "2026-11-01T03-00-07Z.sql.gz"


def _put(bucket: str, key: str) -> dict:
    return {
        "Bucket": bucket,
        "Key": key,
        "Body": ANY,
        "ContentType": "application/gzip",
        "IfNoneMatch": "*",
        "ChecksumAlgorithm": "SHA256",
    }


@pytest.fixture
def s3(monkeypatch):
    client = boto3.client(
        "s3",
        region_name="ap-southeast-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    monkeypatch.setattr(
        job, "build_dump_store", lambda bucket: S3BackupProvider(bucket, client)
    )
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


@pytest.fixture
def pg_dump(monkeypatch):
    """Stands in for the binary: records the call and writes the target file."""
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        target = next(a for a in args if a.startswith("--file="))
        Path(target.removeprefix("--file=")).write_bytes(b"dump")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(job.subprocess, "run", run)
    return calls


@pytest.fixture
def alerts(monkeypatch):
    sent = []

    async def send(subject, body):
        sent.append(subject)

    monkeypatch.setattr(job, "send_backup_alert", send)
    return sent


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        settings,
        "BACKUP_DB_URL",
        "postgresql://backup.user:s3cret%21@pooler.example:5432/postgres?sslmode=require",
    )
    monkeypatch.setattr(settings, "BACKUP_DB_BUCKET", "hourly")
    monkeypatch.setattr(settings, "BACKUP_DB_MONTHLY_BUCKET", "monthly")
    monkeypatch.setattr(job, "utcnow", lambda: NOW)


async def test_a_normal_day_only_fills_the_hourly_bucket(s3, pg_dump):
    s3.add_response("put_object", {}, _put("hourly", KEY))

    await job.backup_database()

    assert len(pg_dump) == 1


async def test_the_first_of_the_month_also_fills_the_monthly_bucket(
    s3, pg_dump, monkeypatch
):
    monkeypatch.setattr(job, "utcnow", lambda: FIRST_OF_MONTH)
    s3.add_response("put_object", {}, _put("hourly", FIRST_OF_MONTH_KEY))
    s3.add_response("put_object", {}, _put("monthly", FIRST_OF_MONTH_KEY))

    await job.backup_database()


async def test_a_monthly_upload_failure_fails_the_job_and_alerts(
    s3, pg_dump, monkeypatch, alerts
):
    monkeypatch.setattr(job, "utcnow", lambda: FIRST_OF_MONTH)
    s3.add_response("put_object", {}, _put("hourly", FIRST_OF_MONTH_KEY))
    s3.add_client_error("put_object", "AccessDenied", http_status_code=403)

    with pytest.raises(ClientError):
        await job.backup_database()

    assert len(alerts) == 1


async def test_the_password_stays_off_the_command_line(s3, pg_dump):
    s3.add_response("put_object", {}, _put("hourly", KEY))

    await job.backup_database()

    args, kwargs = pg_dump[0]
    assert "s3cret" not in " ".join(args)
    assert kwargs["env"]["PGPASSWORD"] == "s3cret!"
    assert (
        "--dbname=postgresql://backup.user@pooler.example:5432/postgres?sslmode=require"
        in args
    )


async def test_a_failed_dump_uploads_nothing_and_alerts(s3, monkeypatch, alerts):
    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="connection refused")

    monkeypatch.setattr(job.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError):
        await job.backup_database()

    assert len(alerts) == 1


async def test_an_unconfigured_environment_skips_the_dump(s3, pg_dump, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DB_BUCKET", None)

    await job.backup_database()

    assert pg_dump == []


def test_the_task_has_its_own_queue_away_from_ocr():
    route = celery_app.amqp.router.route({}, "backup.database")

    assert route["queue"].name == "backups"


def test_the_task_is_scheduled():
    celery_app.loader.import_default_modules()
    schedule = celery_app.conf.beat_schedule["backup-database"]

    assert schedule["task"] == "backup.database"
    assert "backup.database" in celery_app.tasks
