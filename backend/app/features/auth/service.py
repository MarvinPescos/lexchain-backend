from supabase import Client, AuthApiError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.clients import get_supabase_admin
from app.features.users.repository import UserRepository
from .schemas import SignInRequest, SignUpRequest
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import BadGatewayError, BadRequestError, UnauthorizedError


class AuthService:
    """
    Service responsible for authentication and identity management.

    Handles Supabase auth operations and synchronizes user data with the local database.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = UserRepository(session)
        self.supabase: Client = get_supabase_admin()

    async def sign_up(self, data: SignUpRequest) -> dict:
        """
        Register a new user with Supabase and sync to local database.
        Uses a compensating-delete pattern: if the local DB sync fails after
        the Supabase auth user was created, we best-effort delete the
        Supabase user so we don't leave an orphan in auth.users.

        Args:
            data: SignUpRequest containing email, password, and username.

        Returns:
            A dictionary containing the created user's ID and confirmation status.

        Raises:
            BadRequestError: If the user already exists or sign-up fails.
            BadGatewayError: If the Supabase connection fails.
        """
        # 1 Create the user in supabase Auth
        try:
            auth_response = self.supabase.auth.sign_up({
                "email": data.email,
                "password": data.password,
                # "options": {
                #     "data": {
                #         "username": data.username
                #     }
                # }
            })

        except AuthApiError as e:
            if "already registered" in str(e).lower():
                raise BadRequestError("User already registered. Please sign in or request a new verification email.")

            raise BadRequestError(f"Sign up failed: {str(e)}")

        except Exception as e:
            log.error("auth.supabase.signup.error", email=data.email, error=str(e))
            raise BadRequestError(f"Sign up failed: {str(e)}")


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
                is_email_verified=False
            )

        except Exception as sync_err:
            log.error(
                "auth.signup.local_sync_failed",
                supabase_user_id=auth_response.user.id,
                email=data.email,
                error=str(sync_err),
            )
            await self._compensate_delete_supabase_user(auth_response.user.id,)
            # Re-raise so the caller sees the real failure.
            raise


        log.info("auth.user.signed.up", email=data.email)

        requires_confirmation = auth_response.session is None
            
        return {
                "user_id": auth_response.user.id,
                "email": auth_response.user.email,
                "requires_email_confirmation": requires_confirmation
        }
        
    
    async def sign_in(self, data: SignInRequest) -> dict:
        """
        Authenticate a user with Supabase credentials.

        Args:
            data: SignInRequest containing email and password.

        Returns:
            A dictionary containing session tokens and user information.

        Raises:
            UnauthorizedError: If credentials are invalid or email is not verified.
        """

        try:
            auth_response = self.supabase.auth.sign_in_with_password({
                "email": data.email,
                "password": data.password
            })

            if not auth_response.session:
                if auth_response.user:
                    raise UnauthorizedError(
                        "Please verify your email before signing in. Check your inbox for the confirmation link."
                    )
                raise UnauthorizedError("Invalid credentials")
                
            if auth_response.user:
                await self.repo.sync_from_supabase(
                    supabase_user_id=auth_response.user.id,
                    email=auth_response.user.email,
                    is_email_verified=auth_response.user.email_confirmed_at is not None
                )

            user = await self.repo.get_by_supabase_id(auth_response.user.id)
            
            log.info(
                "auth.user.signed.in",
                email=data.email
            )

            return {
                "access_token": auth_response.session.access_token,
                "refresh_token": auth_response.session.refresh_token,
                "expires_in": auth_response.session.expires_in,
                "user": {
                    "id": user.id,
                    "email": auth_response.user.email
                }
            }
        except UnauthorizedError:
            raise
        except AuthApiError as e:
            log.info("auth.signin.rejected", email=data.email, error=str(e))
            raise UnauthorizedError("Invalid credentials")

    def resend_verification_email(self, email: str) -> dict:
        """
        Resend the verification email for an unverified user.

        Args:
            email: The email address to send the verification to.

        Returns:
            A dictionary containing a success message.

        Raises:
            BadRequestError: If the email cannot be sent (e.g., user not found).
        """
        try:
            self.supabase.auth.resend({
                "type": "signup",
                "email": email
                })
            log.info("auth.verification.resent", email=email)
            return {"message": "Verification email sent. Please check your inbox."}
        except AuthApiError as e:
            log.error("auth.resend.error", email=email, error=str(e))
            raise BadRequestError(f"Could not resend verification email: {str(e)}")


    async def _compensate_delete_supabase_user(self, supabase_user_id: str) -> None:
        """
            Best-effor deletion of a Supabase auth user after a failed local sync.


            This is intentionally swallow-all: the caller is already about to
            re-raise the original error, and we don't want cleanup failures to
            mask it. We do log loudly so orphans can be found and reconciled
            later (e.g. when the outbox pattern is introduced).
        """

        try:
            self.supabase.auth.admin.delete_user(supabase_user_id)
            log.warning(
                "auth.signup.compensation.deleted_supabase_user",
                supabase_user_id=supabase_user_id
            )
        except Exception as clean_up_err:
            log.error(
                "auth.signup.compensation.failed",
                supabase_user_id=supabase_user_id,
                error=str(clean_up_err),
            )

