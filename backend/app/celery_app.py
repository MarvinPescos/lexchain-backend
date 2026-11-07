from collections.abc import Callable
from typing import Any, cast

from celery import Celery, Task

from app.core.config import settings
from app.features.blockchain.models import OnChainRecord
from app.features.users.models import User

celery_app = Celery(
    "lexchain",
    broker=settings.CELERY_BROKER_URL,
    include=[
        "app.celery_tasks.notifications",
        "app.celery_tasks.documents",
        "app.celery_tasks.backups",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_default_queue="default",
    task_routes={
        # Wildcard so renaming or adding a document task cannot silently strand
        # it in the default queue, which no worker consumes.
        "document.*": {"queue": "documents"},
        "notifications.*": {"queue": "notifications"},
        # Its own worker: on the -c 1 documents worker a dump and OCR would
        # block each other.
        "backup.*": {"queue": "backups"},
        # A full rescan takes minutes: same reason as above.
        "chain.*": {"queue": "backups"},
    },
    # Draft comments are polled: Drive's file webhooks do not report comments.
    # Run with `celery -A app.celery_app beat`.
    beat_schedule={
        "sync-draft-comments": {
            "task": "document.sync_draft_comments",
            "schedule": settings.DRAFT_COMMENT_SYNC_SECONDS,
        },
        # Processing jobs whose first send failed (broker down).
        "dispatch-pending": {
            "task": "document.dispatch_pending",
            "schedule": settings.DISPATCH_RETRY_SECONDS,
        },
        "retry-draft-revocations": {
            "task": "document.retry_draft_revocations",
            "schedule": settings.DRAFT_REVOCATION_RETRY_SECONDS,
        },
        "verify-backups": {
            "task": "document.verify_backups",
            "schedule": settings.BACKUP_VERIFY_SECONDS,
        },
        "backup-database": {
            "task": "backup.database",
            "schedule": settings.BACKUP_DB_SECONDS,
        },
        "check-replication": {
            "task": "backup.check_replication",
            "schedule": settings.BACKUP_REPLICATION_CHECK_SECONDS,
        },
        "reconcile-chain": {
            "task": "document.reconcile_chain",
            "schedule": settings.CHAIN_RECONCILE_SECONDS,
        },
        "retry-metadata-anchors": {
            "task": "document.retry_metadata_anchors",
            "schedule": settings.METADATA_ANCHOR_RETRY_SECONDS,
        },
        "watch-deletions": {
            "task": "backup.watch_deletions",
            "schedule": settings.DELETION_WATCH_SECONDS,
        },
        "watch-owner-events": {
            "task": "chain.watch_owner_events",
            "schedule": settings.CHAIN_OWNER_WATCH_SECONDS,
        },
    },
)


def celery_task(**options: Any) -> Callable[[Callable[..., Any]], Task]:
    """`celery_app.task`, typed. Celery ships no hints, so a decorated task
    would otherwise read as a plain function with no `.delay`."""
    return cast(Callable[[Callable[..., Any]], Task], celery_app.task(**options))
