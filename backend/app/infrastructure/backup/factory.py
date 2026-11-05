from app.core import settings

from .base import BackupProvider, DumpStore


def build_backup() -> BackupProvider:
    """Return the configured archive destination.

    One place to change when the interim Supabase bucket is replaced by an
    object-locked bucket in a separate account.
    """
    if settings.BACKUP_PROVIDER == "supabase":
        from .supabase_backup import SupabaseBackupProvider

        return SupabaseBackupProvider()

    if settings.BACKUP_PROVIDER == "s3":
        from .s3_backup import S3BackupProvider

        return S3BackupProvider()

    raise ValueError(f"Unsupported BACKUP_PROVIDER: {settings.BACKUP_PROVIDER}")


def build_dump_store(bucket: str) -> DumpStore:
    """Return the locked bucket database dumps go to."""
    from .s3_backup import S3BackupProvider

    return S3BackupProvider(bucket)
