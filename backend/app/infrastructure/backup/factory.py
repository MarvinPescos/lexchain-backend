from app.core import settings

from .base import BackupProvider


def build_backup() -> BackupProvider:
    """Return the configured archive destination.

    One place to change when the interim Supabase bucket is replaced by an
    object-locked bucket in a separate account.
    """
    if settings.BACKUP_PROVIDER == "supabase":
        from .supabase_backup import SupabaseBackupProvider

        return SupabaseBackupProvider()

    raise ValueError(f"Unsupported BACKUP_PROVIDER: {settings.BACKUP_PROVIDER}")
