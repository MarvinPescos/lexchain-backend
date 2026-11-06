import pytest

from app.core import settings
from app.infrastructure.backup import alerts as backup_alerts
from app.workers import check_replication as job
from app.workers.check_replication import SlotState, replication_problem

MAX_LAG = 1000


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        (None, "slot_missing"),
        (SlotState(active=False, lag_bytes=0), "subscription_stopped"),
        (SlotState(active=True, lag_bytes=MAX_LAG + 1), "lag_exceeded"),
        (SlotState(active=True, lag_bytes=None), "lag_exceeded"),
        (SlotState(active=True, lag_bytes=MAX_LAG), None),
    ],
)
def test_replication_problem(slot, expected):
    assert replication_problem(slot, MAX_LAG) == expected


class Outbox:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        return True


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(backup_alerts, "build_email_provider", lambda: box)
    monkeypatch.setattr(settings, "BACKUP_ALERT_EMAIL", "lawyer@example.com")
    return box


def _slot(monkeypatch, state):
    async def read(_session, _slot_name):
        return state

    monkeypatch.setattr(job, "read_slot", read)


async def test_a_problem_emails_the_alert_address(outbox, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_REPLICATION_SLOT", "rds_copy")
    _slot(monkeypatch, None)

    await job.check_replication()

    assert [m.to for m in outbox.sent] == ["lawyer@example.com"]
    assert "slot_missing" in outbox.sent[0].text


async def test_healthy_replication_sends_nothing(outbox, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_REPLICATION_SLOT", "rds_copy")
    _slot(monkeypatch, SlotState(active=True, lag_bytes=0))

    await job.check_replication()

    assert outbox.sent == []


async def test_no_slot_configured_means_no_check(outbox, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_REPLICATION_SLOT", None)

    await job.check_replication()

    assert outbox.sent == []


async def test_read_slot_reports_a_slot_that_does_not_exist(session):
    assert await job.read_slot(session, "no_such_slot") is None
