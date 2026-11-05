import io

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from app.core import settings
from app.infrastructure.backup import build_backup
from app.infrastructure.backup.s3_backup import S3BackupProvider

KEY = "doc-id/abc123.pdf"


@pytest.fixture
def s3():
    client = boto3.client(
        "s3",
        region_name="ap-southeast-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


async def test_store_writes_once_with_a_checksum(s3):
    client, stubber = s3
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": "docs",
            "Key": KEY,
            "Body": ANY,
            "ContentType": "application/pdf",
            "IfNoneMatch": "*",
            "ChecksumAlgorithm": "SHA256",
        },
    )

    ref = await S3BackupProvider("docs", client).store(KEY, b"pdf", "application/pdf")

    assert (ref.key, ref.size_bytes, ref.provider) == (KEY, 3, "s3")


async def test_store_refuses_to_replace_an_existing_key(s3):
    client, stubber = s3
    stubber.add_client_error(
        "put_object", "PreconditionFailed", http_status_code=412
    )

    with pytest.raises(ClientError):
        await S3BackupProvider("docs", client).store(KEY, b"pdf", "application/pdf")


async def test_fetch_returns_the_object_bytes(s3):
    client, stubber = s3
    stubber.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(b"pdf"), 3)},
        {"Bucket": "docs", "Key": KEY},
    )

    assert await S3BackupProvider("docs", client).fetch(KEY) == b"pdf"


async def test_exists_is_true_for_a_present_object(s3):
    client, stubber = s3
    stubber.add_response("head_object", {}, {"Bucket": "docs", "Key": KEY})

    assert await S3BackupProvider("docs", client).exists(KEY) is True


async def test_exists_is_false_only_for_a_404(s3):
    client, stubber = s3
    stubber.add_client_error("head_object", "404", http_status_code=404)

    assert await S3BackupProvider("docs", client).exists(KEY) is False


async def test_exists_raises_on_anything_else_so_an_outage_is_not_a_lost_archive(s3):
    client, stubber = s3
    stubber.add_client_error("head_object", "403", http_status_code=403)

    with pytest.raises(ClientError):
        await S3BackupProvider("docs", client).exists(KEY)


def test_factory_builds_the_s3_provider(monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_PROVIDER", "s3")
    monkeypatch.setattr(settings, "AWS_REGION", "ap-southeast-1")

    assert isinstance(build_backup(), S3BackupProvider)
