import app.all_models  # noqa: F401 — registers all SQLAlchemy models for the worker process
from app.celery_app import celery_task
from app.celery_tasks.runner import run_async


@celery_task(name="backup.database")
def backup_database_task():
    """Dump the whole database to the locked buckets."""
    from app.workers.backup_database import backup_database

    run_async(backup_database())


@celery_task(name="backup.check_replication")
def check_replication_task():
    """Alert when the RDS copy stops keeping up."""
    from app.workers.check_replication import check_replication

    run_async(check_replication())
