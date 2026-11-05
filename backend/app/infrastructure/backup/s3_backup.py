import asyncio
from typing import Any

import boto3
from botocore.exceptions import ClientError

from app.core import settings
from app.infrastructure.observability.logging_setup import log

from .base import BackupRef


class S3BackupProvider:
    """Archive copies in an Object Lock bucket in the separate backups account.

    Credentials come from the default chain, which picks up the instance role.
    """

    provider_name = "s3"

    def __init__(self, bucket_name: str | None = None, client: Any = None) -> None:
        self.bucket_name = bucket_name or settings.BACKUP_BUCKET
        self._client = client or boto3.client("s3", region_name=settings.AWS_REGION)

    async def store(self, key: str, data: bytes, content_type: str) -> BackupRef:
        # boto3 is synchronous; keep it off the event loop. Object Lock rejects
        # a write without a checksum, and the bucket policy one without
        # If-None-Match.
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
            IfNoneMatch="*",
            ChecksumAlgorithm="SHA256",
        )

        log.info(
            "backup.stored",
            provider=self.provider_name,
            bucket=self.bucket_name,
            key=key,
            size_bytes=len(data),
        )
        return BackupRef(key=key, size_bytes=len(data), provider=self.provider_name)

    async def fetch(self, key: str) -> bytes:
        def _download() -> bytes:
            response = self._client.get_object(Bucket=self.bucket_name, Key=key)
            return response["Body"].read()

        data = await asyncio.to_thread(_download)
        log.info(
            "backup.fetched",
            provider=self.provider_name,
            key=key,
            size_bytes=len(data),
        )
        return data

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(
                self._client.head_object, Bucket=self.bucket_name, Key=key
            )
        except ClientError as e:
            # Anything but 404 propagates, so an outage or a missing grant is
            # never read as a lost archive.
            if e.response["Error"]["Code"] == "404":
                return False
            raise
        return True
