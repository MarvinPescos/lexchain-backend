from celery import Celery

from app.core.config import settings
from app.features.blockchain.models import OnChainRecord
from app.features.users.models import User

celery_app = Celery(
    "lexchain",
    broker=settings.CELERY_BROKER_URL,
    include=[
        "app.celery_tasks.notifications",
        "app.celery_tasks.documents",
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
        "document.process": {"queue": "documents"},
        "notifications.*": {"queue": "notifications"},
    },
)
