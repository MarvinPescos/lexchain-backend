from supabase import create_client, Client
from app.core.config import settings
from functools import lru_cache

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