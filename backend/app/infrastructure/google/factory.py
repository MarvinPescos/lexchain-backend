from app.core import settings

from .base import GoogleOAuth


def build_google_oauth() -> GoogleOAuth:
    if not settings.USE_REAL_GOOGLE:
        from .mock_oauth import MockGoogleOAuth

        return MockGoogleOAuth()

    from .http_oauth import HttpGoogleOAuth

    return HttpGoogleOAuth()
