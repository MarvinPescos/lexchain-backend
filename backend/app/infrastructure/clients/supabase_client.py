from functools import lru_cache

from app.core.config import settings
from supabase import Client, ClientOptions, create_client


@lru_cache
def get_supabase_client() -> Client:
    """
        Get supabase client with anon key for (client-side rendering)
        Cached to reuse the same client instance
    """
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)


@lru_cache
def get_supabase_admin() -> Client:
    """
        Get supabase client with service role key for (for admin operations)
        Use this for server-side operation that bypass RLS
    """
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


# Added 2026-09-12 16:59 PST — user auth needs its own throwaway client.
# Signing a user in on a *shared* client mutates that client: gotrue stores the
# session in the instance's storage, starts an auto-refresh timer for it, and
# fires SIGNED_IN, which supabase-py's own listener
# (supabase/_sync/client.py:290) uses to overwrite the client's Authorization
# header with that user's JWT and discard its postgrest/storage/functions
# instances. Done on the lru_cached service-role client, that means every login
# replaced the process-wide admin credential with whichever end user logged in
# last, and left a timer refreshing their session indefinitely.
#
# persist_session/auto_refresh_token=False stop the storing and the timer, but
# not the header swap — SIGNED_IN fires either way — so the client must also be
# per-call rather than cached. These endpoints are low-volume, so building one
# is the cheap side of the trade.
#
# Anon key, not service role: signing in and signing up are exactly what the
# anon key is for, and it keeps the omnipotent key off the paths that accept
# unauthenticated input.
def build_supabase_auth_client() -> Client:
    """Build a single-use anon client for one end-user auth call."""
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_ANON_KEY,
        options=ClientOptions(persist_session=False, auto_refresh_token=False),
    )
