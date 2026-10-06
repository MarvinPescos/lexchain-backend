import pytest

from app.features.documents import lifecycle
from app.features.documents.constants import (
    ACTION_ATTACH_SIGNED_COPY,
    ACTION_CANCEL,
    ACTION_FINALIZE,
    ACTION_OPEN,
    ACTION_UPLOAD_SIGNED,
    LIFECYCLES,
)
from app.features.documents.models import Document
from app.shared.errors.exceptions import ConflictError

ALLOWED = {
    (action, stage)
    for action, (sources, _) in lifecycle.TRANSITIONS.items()
    for stage in sources
    if stage is not None
}
STAGE_ACTIONS = [
    a for a in lifecycle.TRANSITIONS if a not in (ACTION_OPEN, ACTION_UPLOAD_SIGNED)
]


@pytest.mark.parametrize("stage", LIFECYCLES)
@pytest.mark.parametrize("action", STAGE_ACTIONS)
def test_only_listed_transitions_are_allowed(action, stage):
    allowed = lifecycle.can(action, Document(lifecycle=stage))
    assert allowed == ((action, stage) in ALLOWED)


def test_attach_from_preparing_says_to_mark_ready():
    with pytest.raises(ConflictError, match="Mark the document Ready for signature"):
        lifecycle.ensure(ACTION_ATTACH_SIGNED_COPY, Document(lifecycle="PREPARING"))


def test_signed_document_cannot_be_cancelled():
    assert not lifecycle.can(ACTION_CANCEL, Document(lifecycle="SIGNED"))


def test_finalize_only_from_signed():
    allowed = [
        s for s in LIFECYCLES if lifecycle.can(ACTION_FINALIZE, Document(lifecycle=s))
    ]
    assert allowed == ["SIGNED"]
