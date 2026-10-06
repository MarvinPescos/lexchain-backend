from datetime import datetime

from pydantic import BaseModel, Field


class GoogleConnectResponse(BaseModel):
    authorization_url: str = Field(
        ..., description="Send the browser here; Google redirects back to /google/callback"
    )


class GoogleConnectionStatus(BaseModel):
    connected: bool
    google_email: str | None = Field(None, description="The connected Google account")
    connected_at: datetime | None = None


class GooglePickerToken(BaseModel):
    """What the frontend's Google Picker needs to show the lawyer's Drive."""

    access_token: str
    expires_at: datetime
