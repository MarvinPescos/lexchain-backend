from functools import partial

from gotrue.helpers import model_validate
from gotrue.types import (
    AuthMFAChallengeResponse,
    AuthMFAEnrollResponse,
    AuthMFAVerifyResponse,
    Factor,
)

from .supabase_client import build_supabase_auth_client

# The issuer an authenticator app shows next to the account.
TOTP_ISSUER = "LexChain"


# Added 2026-11-08 — MFA calls made with the caller's own JWT.
# supabase-py's public mfa.* methods read the session stored on the client, and
# this backend never stores one: the frontend holds its tokens and sends the
# access token per request (see build_supabase_auth_client for why a client
# must not carry a session). Loading one with set_session would also need the
# refresh token on every MFA call. GoTrue's REST endpoints only need the JWT,
# so these go through the client's `_request` with `jwt=` — the same call the
# public methods make once they have found a session. It is private API, pinned
# through supabase's version in requirements; the tests fake this class.
class SupabaseMfa:
    """GoTrue's TOTP factor endpoints, called as the user who owns `jwt`."""

    def enroll_totp(self, jwt: str) -> AuthMFAEnrollResponse:
        response = build_supabase_auth_client().auth._request(
            "POST",
            "factors",
            body={"factor_type": "totp", "issuer": TOTP_ISSUER},
            jwt=jwt,
            xform=partial(model_validate, AuthMFAEnrollResponse),
        )
        # Same prefix mfa.enroll adds, so the SVG drops straight into an <img>.
        if response.totp and response.totp.qr_code:
            response.totp.qr_code = (
                f"data:image/svg+xml;utf-8,{response.totp.qr_code}"
            )
        return response

    def challenge_and_verify(
        self, jwt: str, factor_id: str, code: str
    ) -> AuthMFAVerifyResponse:
        auth = build_supabase_auth_client().auth
        challenge = auth._request(
            "POST",
            f"factors/{factor_id}/challenge",
            body={},
            jwt=jwt,
            xform=partial(model_validate, AuthMFAChallengeResponse),
        )
        return auth._request(
            "POST",
            f"factors/{factor_id}/verify",
            body={"challenge_id": challenge.id, "code": code},
            jwt=jwt,
            xform=partial(model_validate, AuthMFAVerifyResponse),
        )

    def list_factors(self, jwt: str) -> list[Factor]:
        # get_user is public and takes the JWT directly.
        response = build_supabase_auth_client().auth.get_user(jwt)
        return (response.user.factors if response else None) or []

    def unenroll(self, jwt: str, factor_id: str) -> None:
        build_supabase_auth_client().auth._request(
            "DELETE", f"factors/{factor_id}", jwt=jwt
        )
