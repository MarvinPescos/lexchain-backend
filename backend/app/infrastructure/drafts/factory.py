from app.core import settings

from .base import DraftWorkspace


def build_draft_workspace() -> DraftWorkspace:
    if not settings.USE_REAL_GOOGLE:
        from .mock_workspace import MockDraftWorkspace

        return MockDraftWorkspace()

    from .google_docs import GoogleDocsWorkspace

    return GoogleDocsWorkspace()
