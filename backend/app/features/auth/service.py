import asyncio

from app.features.users.repository import UserRepository
from app.infrastructure.clients import build_supabase_auth_client, get_supabase_admin
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import (
    BadGatewayError,
    BadRequestError,
    UnauthorizedError,
)
from sqlalchemy.ext.asyncio import AsyncSession
from supabase import AuthApiError, Client

from .schemas import (
    MessageResponse,
    SignInRequest,
    SignInResponse,
    SignInUser,
    SignUpRequest,
    SignUpResponse,
)

# Added 2026-09-14 19:55 PST — one home for the resend reply.
# It is returned on every path through resend_verification_email, success and
# failure alike, and the whole point is that the caller cannot tell them apart.
# Keeping the two branches from re-typing the string is what keeps that true.
_RESEND_MESSAGE = (
    "If that address has an unverified account, "
    "a verification email has been sent. Please check your inbox."
)


# Added 2026-09-07 00:01 PST — keep plaintext addresses out of the log stream.
# Every auth event logged `email=` verbatim, successes and failures alike. On a
# legal platform the client roster is itself sensitive, and these lines travel
# to wherever logs are shipped. Enough is kept to correlate a report with a
# specific account without printing the address.
def _mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return "<unknown>"
    local, _, domain = email.partition("@")
    return f"{local[0]}***@{domain}"


class AuthService:
    """
    Service responsible for authentication and identity management.

    Handles Supabase auth operations and synchronizes user data with the local database.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = UserRepository(session)
        # Added 2026-09-12 16:59 PST — split the two clients apart.
        # `self.supabase` was the lru_cached service-role client and every
        # end-user call ran on it, so each sign-in overwrote the shared admin
        # credential with that user's JWT (see supabase_client.py). Sign-up,
        # sign-in and resend now each build their own throwaway anon client;
        # only auth.admin.* still needs the service role.
        self.supabase_admin: Client = get_supabase_admin()

    async def sign_up(self, data: SignUpRequest) -> SignUpResponse:
        """
        Register a new user with Supabase and sync to local database.
        Uses a compensating-delete pattern: if the local DB sync fails after
        the Supabase auth user was created, we best-effort delete the
        Supabase user so we don't leave an orphan in auth.users.

        Args:
            data: SignUpRequest containing email, password and name.

        Returns:
            A `SignUpResponse` with the new user's identity and whether they
            still have to confirm their email.

        Raises:
            BadRequestError: If the user already exists or sign-up fails.
            BadGatewayError: If the Supabase connection fails.
        """
        # 1 Create the user in supabase Auth
        try:
            # Added 2026-09-14 19:55 PST — off the event loop.
            # supabase-py's client is synchronous: this is a blocking HTTPS
            # round-trip to GoTrue made from inside an async handler, so it
            # stalled *every* in-flight request for its duration, not just its
            # own. Same treatment as the JWKS fetch in
            # security/dependencies.py and the storage calls in
            # infrastructure/backup/supabase_backup.py.
            auth_response = await asyncio.to_thread(
                build_supabase_auth_client().auth.sign_up,
                {
                    "email": data.email,
                    "password": data.password,
                },
            )

        # Added 2026-09-07 00:01 PST — stop echoing upstream errors to the client.
        # Both branches interpolated the raw Supabase/GoTrue error into the 400
        # body, handing the caller provider internals and whatever detail the
        # upstream happened to include. The detail is logged server-side now and
        # the client gets a fixed message.
        # The "already registered" branch is kept on purpose: it does confirm an
        # address has an account, but this is a single-firm deployment where
        # sign-up is limited to staff and their clients, and the alternative — a
        # fake success — strands a real user with no way to recover their
        # account. Documented rather than silently traded away.
        except AuthApiError as e:
            log.warning(
                "auth.signup.rejected", email=_mask_email(data.email), error=str(e)
            )
            if "already registered" in str(e).lower():
                raise BadRequestError(
                    "User already registered. Please sign in or request a new verification email."
                )

            raise BadRequestError(
                "Sign up failed. Please check your details and try again."
            )

        # Changed 2026-09-14 19:55 PST — 502, not 400.
        # AuthApiError above is GoTrue telling us the *request* was bad, which
        # is the caller's fault and a 400. Everything that lands here instead —
        # DNS, TLS, connection refused, a 5xx from the provider — is the
        # upstream being unreachable, which is not something the caller can fix
        # by editing their details. It was reported as "check your details and
        # try again", which sent users round a loop that could not succeed.
        # This is the case the docstring has always promised BadGatewayError
        # for, and the import was otherwise unused.
        except Exception as e:
            log.error(
                "auth.supabase.signup.error",
                email=_mask_email(data.email),
                error=str(e),
            )
            raise BadGatewayError(
                "Sign up is unavailable right now. Please try again later."
            )

        if not auth_response.user:
            raise BadRequestError("Failed to create user")

        # Step 2: Sync to local DB. If this fails, compensate by deleting
        # the Supabase auth user we just created.
        try:
            # Sync user to local database
            await self.repo.sync_from_supabase(
                supabase_user_id=auth_response.user.id,
                email=data.email,
                f_name=data.f_name,
                l_name=data.l_name,
                is_email_verified=False,
            )

        except Exception as sync_err:
            log.error(
                "auth.signup.local_sync_failed",
                supabase_user_id=auth_response.user.id,
                email=_mask_email(data.email),
                error=str(sync_err),
            )
            await self._compensate_delete_supabase_user(
                auth_response.user.id,
            )
            # Re-raise so the caller sees the real failure.
            raise

        log.info("auth.signup.succeeded", email=_mask_email(data.email))

        requires_confirmation = auth_response.session is None

        # Changed 2026-09-14 19:55 PST — build the response here, not in the router.
        # sign_up/sign_in/resend each returned a bare dict that the router
        # unpacked back into the response model, which left this feature the
        # only one whose service is untyped (documents, users and notifications
        # all return the schema and their routers are one-liners). The sign-up
        # *wording* was picked in the router too — a branch on
        # requires_email_confirmation, i.e. business logic above the service
        # line, where nothing that tests the service can reach it.
        return SignUpResponse(
            message=(
                "Account created successfully. Please check your email to verify your account."
                if requires_confirmation
                else "Account created successfully."
            ),
            user_id=auth_response.user.id,
            email=auth_response.user.email,
            requires_email_confirmation=requires_confirmation,
        )

    async def sign_in(self, data: SignInRequest) -> SignInResponse:
        """
        Authenticate a user with Supabase credentials.

        Args:
            data: SignInRequest containing email and password.

        Returns:
            A `SignInResponse` containing the session tokens and the signed-in
            user.

        Raises:
            UnauthorizedError: If credentials are invalid, the email is not
                verified, or the account is deactivated.
            BadGatewayError: If the Supabase connection fails.
        """

        try:
            # Blocking call — see the note in sign_up.
            auth_response = await asyncio.to_thread(
                build_supabase_auth_client().auth.sign_in_with_password,
                {"email": data.email, "password": data.password},
            )
        except AuthApiError as e:
            log.info(
                "auth.signin.rejected", email=_mask_email(data.email), error=str(e)
            )
            raise UnauthorizedError("Invalid credentials")
        # Added 2026-09-14 19:55 PST — cover the upstream being down.
        # sign_in caught only AuthApiError, so a GoTrue outage left a raw
        # connection error to escape as a 500 while the identical failure in
        # sign_up was handled. Same 502 as there.
        except Exception as e:
            log.error(
                "auth.supabase.signin.error",
                email=_mask_email(data.email),
                error=str(e),
            )
            raise BadGatewayError(
                "Sign in is unavailable right now. Please try again later."
            )

        # Changed 2026-09-14 19:55 PST — establish the Supabase user once.
        # The old flow tested `auth_response.user` for truthiness, then
        # dereferenced `auth_response.user.id` twice below without a guard, so
        # the one shape it defended against would have crashed a few lines
        # later anyway — an AttributeError surfacing as a 500 on the login
        # endpoint. A session without a user is not a state we can do anything
        # with, so it is turned away here as bad credentials.
        supabase_user = auth_response.user

        if not auth_response.session:
            # Not an enumeration oracle despite the differing message: this
            # branch is only reachable after sign_in_with_password returned
            # without raising, i.e. the caller already supplied valid
            # credentials. Wrong password / unknown address raise
            # AuthApiError and collapse to "Invalid credentials" above.
            if supabase_user:
                raise UnauthorizedError(
                    "Please verify your email before signing in. Check your inbox for the confirmation link."
                )
            raise UnauthorizedError("Invalid credentials")

        if not supabase_user:
            raise UnauthorizedError("Invalid credentials")

        await self.repo.sync_from_supabase(
            supabase_user_id=supabase_user.id,
            email=supabase_user.email,
            is_email_verified=supabase_user.email_confirmed_at is not None,
        )

        user = await self.repo.get_by_supabase_id(supabase_user.id)

        # Added 2026-09-14 19:55 PST — sync_from_supabase is not a promise.
        # `user.is_active` was read straight off this lookup. sync_from_supabase
        # writes within the request's transaction and this re-reads it, so a
        # None here means the write did not take — and the old code turned that
        # into an AttributeError, i.e. a 500 whose traceback says nothing about
        # what actually went wrong. Fail as an auth failure and log the reason.
        if not user:
            log.error(
                "auth.signin.local_user_missing",
                supabase_user_id=supabase_user.id,
                email=_mask_email(data.email),
            )
            raise UnauthorizedError("Account is not provisioned. Please try again.")

        # Added 2026-09-06 14:42 PST — refuse tokens to deactivated accounts.
        # The guard in get_current_user is not enough on its own: without this,
        # a deactivated user could still sign in and be issued a fresh token
        # pair on demand, forever. Supabase tokens are also refreshed
        # client-side against GoTrue, so expiry is not a lock-out either — the
        # account has to be turned away at the point of issue as well.
        if not user.is_active:
            log.warning(
                "auth.signin.rejected.inactive_account",
                email=_mask_email(data.email),
            )
            raise UnauthorizedError(
                "Account is deactivated. Contact your administrator."
            )

        log.info("auth.signin.succeeded", email=_mask_email(data.email))

        return SignInResponse(
            access_token=auth_response.session.access_token,
            refresh_token=auth_response.session.refresh_token,
            expires_in=auth_response.session.expires_in,
            user=SignInUser(
                id=user.id,
                email=supabase_user.email,
                role=user.role,
            ),
        )

    # Added 2026-09-14 20:40 PST — there was no way to end a session.
    # Sign-in mints an access token *and* a refresh token and hands both to the
    # client (see SignInResponse), and the frontend holds them directly —
    # docs/frontend-api.md tells it to send `Authorization: Bearer
    # <access_token>` itself rather than going through supabase-js. So nothing
    # anywhere could revoke a session: "log out" could only mean the client
    # dropping its own copy of the tokens. The access token is stateless and
    # dies with its `exp`, but the refresh token stays live on GoTrue and keeps
    # minting new access tokens for as long as the session exists — so a token
    # lifted from a shared machine, a synced browser profile or a backup
    # outlived the logout that was supposed to end it. On a platform holding
    # client legal documents that is the session that matters.
    #
    # This does not touch the deactivation guards: those already cover the
    # access token per request (get_current_user) and re-issue (sign_in). This
    # covers the ordinary case of a user ending their own session.
    async def sign_out(
        self, access_token: str, user: AuthenticatedUser
    ) -> MessageResponse:
        """
        Revoke the caller's Supabase session.

        Args:
            access_token: The bearer token the request authenticated with.
            user: The authenticated caller, for logging.

        Returns:
            A `MessageResponse` confirming the session was ended.

        Raises:
            BadGatewayError: If Supabase is unreachable, i.e. the session could
                not actually be revoked.
        """
        # scope="local" — end *this* session, not every session the user has.
        # GoTrue's own default is "global", which would sign the user out of
        # their phone because they closed a laptop tab. Global belongs behind a
        # deliberate "sign out everywhere" action, not the logout button.
        #
        # admin.sign_out(jwt) rather than a client-side sign_out(): it takes the
        # caller's JWT per request and builds its headers fresh
        # (gotrue _request: `headers = {**self._headers, ...}`), so it neither
        # needs a session loaded onto a client nor mutates the shared admin one
        # — which is the whole failure documented in supabase_client.py.
        try:
            # Blocking call — see the note in sign_up.
            await asyncio.to_thread(
                self.supabase_admin.auth.admin.sign_out, access_token, "local"
            )
        # An already-dead session is a successful logout. require_auth has
        # verified the token before we get here, so this is GoTrue saying the
        # session is already gone — which is the state the caller asked for.
        # Logging out twice must not be an error.
        except AuthApiError as e:
            log.info(
                "auth.signout.already_ended",
                user_id=str(user.user_id),
                error=str(e),
            )
        # Reported, not swallowed: if this call did not reach GoTrue the
        # refresh token is still live, and answering 200 would tell the client
        # the session is over when it is not. The client should still clear its
        # local tokens either way.
        except Exception as e:
            log.error(
                "auth.supabase.signout.error",
                user_id=str(user.user_id),
                error=str(e),
            )
            raise BadGatewayError(
                "Could not end the session right now. Please try again."
            )
        else:
            log.info("auth.signout.succeeded", user_id=str(user.user_id))

        return MessageResponse(message="Signed out.")

    async def resend_verification_email(self, email: str) -> MessageResponse:
        """
        Resend the verification email for an unverified user.

        Args:
            email: The email address to send the verification to.

        Returns:
            A `MessageResponse` carrying a neutral message. The same message is
            returned whether or not the address has an unverified account.
        """
        # Added 2026-09-07 00:01 PST — answer identically either way.
        # This endpoint takes no auth dependency and (until the rate limiter
        # lands) no throttle, and it returned the raw Supabase error to the
        # caller — which distinguished unknown address / already verified /
        # upstream throttled, one request at a time, from anyone on the
        # internet. The outcome is logged server-side instead and the response
        # no longer varies, so the caller learns nothing about who has an
        # account here.
        #
        # Changed 2026-09-14 19:55 PST — `async def`, and the call off the
        # event loop. It was the one synchronous service method in the codebase
        # and the router called it without awaiting, which meant a blocking
        # GoTrue round-trip ran inline on the loop. Made async both to stop
        # that and so the method does not read as the odd one out.
        try:
            await asyncio.to_thread(
                build_supabase_auth_client().auth.resend,
                {"type": "signup", "email": email},
            )
            log.info("auth.verification.resent", email=_mask_email(email))
        except AuthApiError as e:
            log.warning("auth.resend.failed", email=_mask_email(email), error=str(e))
        # Added 2026-09-14 19:55 PST — swallow upstream failures too.
        # Only AuthApiError was caught, so an unreachable GoTrue escaped as a
        # 500 while every other outcome answered with the neutral message —
        # which handed back exactly the distinction this endpoint exists to
        # hide, and turned a provider outage into an error on a path where the
        # caller can do nothing anyway. Logged, not reported.
        except Exception as e:
            log.error(
                "auth.supabase.resend.error", email=_mask_email(email), error=str(e)
            )

        return MessageResponse(message=_RESEND_MESSAGE)

    async def _compensate_delete_supabase_user(self, supabase_user_id: str) -> None:
        """
        Best-effor deletion of a Supabase auth user after a failed local sync.


        This is intentionally swallow-all: the caller is already about to
        re-raise the original error, and we don't want cleanup failures to
        mask it. We do log loudly so orphans can be found and reconciled
        later (e.g. when the outbox pattern is introduced).
        """

        try:
            # Blocking call — see the note in sign_up.
            await asyncio.to_thread(
                self.supabase_admin.auth.admin.delete_user, supabase_user_id
            )
            log.warning(
                "auth.signup.compensation.deleted_supabase_user",
                supabase_user_id=supabase_user_id,
            )
        except Exception as clean_up_err:
            log.error(
                "auth.signup.compensation.failed",
                supabase_user_id=supabase_user_id,
                error=str(clean_up_err),
            )
