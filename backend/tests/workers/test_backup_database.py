import subprocess
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from app.celery_app import celery_app
from app.core import settings
from app.workers import backup_database as job

NOW = datetime(2026, 11, 5, 3, 0, 7, tzinfo=timezone.utc)
HOURLY_KEY = "2026-11-05T03-00-07Z.sql.gz"
MONTHLY_KEY = "2026-11.sql.gz"


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
    monkeypatch.setattr(job.boto3, "client", lambda *a, **k: client)
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


def test_dump_goes_to_the_hourly_and_monthly_buckets(s3, pg_dump):
    s3.add_response("put_object", {}, _put("hourly", HOURLY_KEY))
    s3.add_response("put_object", {}, _put("monthly", MONTHLY_KEY))

    job.backup_database()

    assert len(pg_dump) == 1


def test_a_month_already_stored_is_not_a_failure(s3, pg_dump):
    s3.add_response("put_object", {}, _put("hourly", HOURLY_KEY))
    s3.add_client_error("put_object", "PreconditionFailed", http_status_code=412)

    job.backup_database()


def test_a_monthly_upload_failure_fails_the_job(s3, pg_dump):
    s3.add_response("put_object", {}, _put("hourly", HOURLY_KEY))
    s3.add_client_error("put_object", "AccessDenied", http_status_code=403)

    with pytest.raises(ClientError):
        job.backup_database()


def test_the_password_stays_off_the_command_line(s3, pg_dump):
    s3.add_response("put_object", {}, _put("hourly", HOURLY_KEY))
    s3.add_response("put_object", {}, _put("monthly", MONTHLY_KEY))

    job.backup_database()

    args, kwargs = pg_dump[0]
    assert "s3cret" not in " ".join(args)
    assert kwargs["env"]["PGPASSWORD"] == "s3cret!"
    assert (
        "--dbname=postgresql://backup.user@pooler.example:5432/postgres?sslmode=require"
        in args
    )


def test_a_failed_dump_uploads_nothing(s3, monkeypatch):
    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="connection refused")

    monkeypatch.setattr(job.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError):
        job.backup_database()


def test_an_unconfigured_environment_skips_the_dump(s3, pg_dump, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DB_BUCKET", None)

    job.backup_database()

    assert pg_dump == []


def test_the_task_has_its_own_queue_away_from_ocr():
    route = celery_app.amqp.router.route({}, "backup.database")

    assert route["queue"].name == "backups"


def test_the_task_is_scheduled():
    celery_app.loader.import_default_modules()
    schedule = celery_app.conf.beat_schedule["backup-database"]

    assert schedule["task"] == "backup.database"
    assert "backup.database" in celery_app.tasks
