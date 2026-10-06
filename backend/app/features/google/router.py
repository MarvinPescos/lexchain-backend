from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import RedirectResponse

from app.core import settings
from app.infrastructure.security.dependencies import AuthenticatedUser, require_lawyer
from app.shared.errors.exceptions import BadRequestError

from .dependencies import get_google_service
from .schemas import GoogleConnectionStatus, GoogleConnectResponse, GooglePickerToken
from .service import GoogleConnectionService

router = APIRouter(prefix="/google", tags=["Google"])


@router.post(
    "/connect",
    response_model=GoogleConnectResponse,
    summary="Start connecting a Google account",
    description=(
        "Returns Google's consent URL. Send the lawyer's browser there; Google "
        "redirects back to `/google/callback`, which then redirects to the app."
    ),
)
async def connect(
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: GoogleConnectionService = Depends(get_google_service),
) -> GoogleConnectResponse:
    return service.start(current_user.user_id)


@router.get(
    "/callback",
    status_code=status.HTTP_302_FOUND,
    summary="Google's OAuth redirect target",
    description=(
        "Called by Google, not the app — there is no bearer token; the `state` "
        "identifies the lawyer. Redirects to GOOGLE_CONNECTED_REDIRECT with "
        "`?google=connected` or `?google=error&message=...`."
    ),
)
async def callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None, description="Set by Google when consent was denied"),
    service: GoogleConnectionService = Depends(get_google_service),
) -> RedirectResponse:
    target = settings.GOOGLE_CONNECTED_REDIRECT
    if error or not code or not state:
        query = {"google": "error", "message": error or "Google sign-in was cancelled"}
        return RedirectResponse(f"{target}?{urlencode(query)}", status.HTTP_302_FOUND)
    try:
        await service.complete(code, state)
    except BadRequestError as e:
        query = {"google": "error", "message": e.message}
        return RedirectResponse(f"{target}?{urlencode(query)}", status.HTTP_302_FOUND)
    return RedirectResponse(f"{target}?google=connected", status.HTTP_302_FOUND)


@router.get(
    "/connection",
    response_model=GoogleConnectionStatus,
    summary="Is a Google account connected?",
)
async def get_connection(
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: GoogleConnectionService = Depends(get_google_service),
) -> GoogleConnectionStatus:
    return await service.status(current_user.user_id)


@router.delete(
    "/connection",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect Google",
    description="Revokes LexChain's access. Existing drafts stay in the lawyer's Drive.",
)
async def disconnect(
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: GoogleConnectionService = Depends(get_google_service),
) -> None:
    await service.disconnect(current_user.user_id)


@router.get(
    "/picker-token",
    response_model=GooglePickerToken,
    summary="Token for the Google Picker",
    description=(
        "A short-lived access token for the frontend's Google Picker, used to "
        "choose an existing Doc or a template. Picking a file is what grants "
        "LexChain access to it."
    ),
)
async def picker_token(
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: GoogleConnectionService = Depends(get_google_service),
) -> GooglePickerToken:
    return await service.picker_token(current_user.user_id)
