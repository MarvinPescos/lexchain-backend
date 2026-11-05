from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database Configuration
    DB_USER: str = Field(..., description="Database Username")
    DB_PASS: str = Field(..., min_length=1, description="Database password")
    DB_HOST: str = Field(..., description="Database host")
    DB_PORT: str = Field(..., pattern=r"\d+$", description="Database port")
    DB_NAME: str = Field(..., min_length=1, description="Database name")

    # Connection URLs
    DB_URL: str = Field(..., description="Database URL")
    TEST_DB_URL: str = Field(..., description="Database URL for testing")

    # Session pool
    DB_POOL_SIZE: int = Field(default=5, description="Connection pool size")
    DB_MAX_OVERFLOW: int = Field(
        default=0, description="Max overflow connections beyond pool_size"
    )
    DB_POOL_PRE_PING: bool = Field(
        default=False, description="Verify connections before checkout"
    )
    DB_POOL_RECYCLE: int = Field(
        default=-1, description="Recycle connections after N seconds (-1 = never)"
    )
    DB_ECHO: bool = Field(default=False, description="Echo SQL statements to log")

    # Application Settings
    ENVIRONMENT: str = Field(..., description="development or production")

    # Supabase Config
    SUPABASE_URL: str = Field(..., description="Supabase project URL")
    SUPABASE_ANON_KEY: str = Field(..., description="Supabase anonymous key")
    SUPABASE_SERVICE_ROLE_KEY: str = Field(
        ..., description="Supabase role key for admin operations"
    )
    # SUPABASE_JWT_SECRET: str = Field(..., description="Supabase JWT secret for token verification") #Not been used I think

    # LLM — all real inference goes through OpenRouter.
    # Accepts OPEN_ROUTER as well, which is what the existing .env uses.
    OPENROUTER_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("OPENROUTER_API_KEY", "OPEN_ROUTER"),
        description="OpenRouter API key",
    )
    OPENROUTER_BASE_URL: str = Field(
        default="https://openrouter.ai/api/v1", description="OpenRouter API base URL"
    )
    OPENROUTER_MODEL: str = Field(
        default="meta-llama/llama-3.3-70b-instruct",
        description="Model slug, e.g. meta-llama/llama-3.3-70b-instruct",
    )
    OPENROUTER_SITE_URL: str = Field(
        default="", description="Optional HTTP-Referer sent to OpenRouter"
    )
    OPENROUTER_APP_NAME: str = Field(
        default="LexChain", description="Optional X-Title sent to OpenRouter"
    )
    USE_REAL_LLM: bool = Field(
        default=True, description="False routes every LLM call to the mock provider"
    )

    # Legacy providers — kept optional so existing .env files still load, but
    # nothing constructs them any more.
    GROQ_API_KEY: str = Field(default="", description="Unused: superseded by OpenRouter")
    NVIDIA_API_KEY: str = Field(default="", description="Unused: superseded by OpenRouter")

    # Embedder
    USE_REAL_EMBEDDER: bool = Field(default=False, description="Basta Embedder")
    EMBEDDER_ENGINE: str = Field(default="e5", description="e5 | sbert")
    E5_MODEL: str = "intfloat/e5-base-v2"
    SBERT_MODEL: str = "all-MiniLM-L6-v2"

    # OCR
    USE_REAL_OCR: bool = Field(default=False, description="Use real OCR providers")
    OCR_ENGINE: str = Field(default="pypdf", description="pypdf | mineru | marker")

    CHUNKER_ENGINE: str = Field(..., description="Engine would be used in chunking")

    # MinerU
    MINERU_MODE: str = Field(default="api", description="MinerU mode: api")
    MINERU_API_URL: str = Field(
        default="http://localhost:8888", description="MinerU API URL"
    )
    MINERU_LANG: str = Field(
        default="en", description="OCR language sent to MinerU (its own default is 'ch')"
    )
    MINERU_BACKEND: str = Field(
        default="pipeline", description="MinerU backend: pipeline | vlm-engine"
    )
    MINERU_TIMEOUT: int = Field(
        default=120, description="MinerU API timeout in seconds"
    )

    # Marker
    MARKER_API_URL: str = Field(
        default="http://localhost:8001", description="marker_server URL"
    )
    MARKER_FORCE_OCR: bool = Field(
        default=False, description="OCR every page even where the PDF has a text layer"
    )
    MARKER_TIMEOUT: int = Field(default=900, description="Marker API timeout in seconds")

    # Backup / recovery
    BACKUP_ENABLED: bool = Field(
        default=True, description="Archive finalized documents so they can be restored"
    )
    BACKUP_PROVIDER: str = Field(
        default="supabase",
        description=(
            "Archive destination. 'supabase' is an interim same-project bucket — "
            "it survives accidental deletion but not a compromised account. "
            "Replace with an object-locked bucket in a separate account."
        ),
    )
    BACKUP_BUCKET: str = Field(
        default="DocumentBackups", description="Bucket holding archived originals"
    )
    BACKUP_VERIFY_SECONDS: int = Field(
        default=86400,
        ge=3600,
        description="How often every archive is checked against its anchored hash",
    )
    AWS_REGION: str = Field(
        default="ap-southeast-1", description="Region of the backup buckets"
    )

    # Database dumps. Unset in development, where there is nothing to protect.
    BACKUP_DB_URL: str | None = Field(
        default=None,
        description=(
            "Plain postgresql:// URL for pg_dump, through Supabase's session "
            "pooler. The transaction pooler does not work with pg_dump."
        ),
    )
    BACKUP_DB_BUCKET: str | None = Field(
        default=None, description="Locked bucket for hourly dumps"
    )
    BACKUP_DB_MONTHLY_BUCKET: str | None = Field(
        default=None, description="Locked bucket for the dumps of each month's first day"
    )
    BACKUP_DB_SECONDS: int = Field(
        default=3600, ge=300, description="How often the database is dumped"
    )
    BACKUP_DB_TIMEOUT_SECONDS: int = Field(
        default=1800, ge=60, description="How long one pg_dump may run"
    )

    # Replication to the RDS copy.
    BACKUP_REPLICATION_SLOT: str | None = Field(
        default=None, description="Replication slot of the RDS subscription"
    )
    BACKUP_REPLICATION_CHECK_SECONDS: int = Field(
        default=900, ge=60, description="How often replication is checked"
    )
    BACKUP_REPLICATION_MAX_LAG_BYTES: int = Field(
        default=64 * 1024 * 1024,
        ge=0,
        description="Replication lag that triggers an alert",
    )
    BACKUP_ALERT_EMAIL: str | None = Field(
        default=None, description="Who is told when a dump fails or replication stops"
    )

    CHAIN_SCAN_FROM_BLOCK: int | None = Field(
        default=None,
        ge=0,
        description=(
            "The contract's deployment block. Chain reconciliation is skipped "
            "without it, rather than scanning from block 0. Each environment "
            "needs its own contract, or another's anchors read as document_missing"
        ),
    )
    CHAIN_LOG_CHUNK_BLOCKS: int = Field(
        default=190,
        ge=1,
        description=(
            "Blocks per get_logs call. Must stay under the RPC provider's cap: "
            "the current WEB_RPC_URL provider refuses a range over 200"
        ),
    )
    CHAIN_RECONCILE_SECONDS: int = Field(
        default=604800,
        ge=3600,
        description="How often the database is reconciled against the chain",
    )

    WEB_RPC_URL: str = Field(..., description="")
    CONTRACT_ADDRESS: str = Field(..., description="")
    CONTRACT_OWNER_PRIVATE_KEY: str = Field(..., description="")

    # On by default so development keeps sending through Resend as before;
    # set false to log emails instead of sending them.
    USE_REAL_EMAIL: bool = Field(default=True, description="Send email through Resend")
    RESEND_API_KEY: str = Field(..., min_length=1, description="Resend API key")
    RESEND_FROM_EMAIL: str = Field(
        default="noreply@lexchain.app", description="Sender address"
    )

    CELERY_BROKER_URL: str = Field(..., description="Broker URL")

    # Google Docs drafting. Off by default: the mock Drive and a sign-in that
    # skips Google's consent screen are used instead.
    USE_REAL_GOOGLE: bool = Field(default=False, description="Use the real Google APIs")
    GOOGLE_CLIENT_ID: str = Field(default="", description="OAuth client ID")
    GOOGLE_CLIENT_SECRET: str = Field(default="", description="OAuth client secret")
    GOOGLE_REDIRECT_URI: str = Field(
        default="http://localhost:8000/google/callback",
        description="Must match the redirect URI registered on the OAuth client",
    )
    GOOGLE_TOKEN_KEY: str = Field(
        default="",
        description=(
            "Fernet key encrypting stored Google tokens and the OAuth state. "
            "Generate with: python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'"
        ),
    )
    DRAFT_COMMENT_SYNC_SECONDS: int = Field(
        default=300, ge=30, description="How often draft comments are pulled from Google Docs"
    )
    DISPATCH_RETRY_SECONDS: int = Field(
        default=60,
        ge=10,
        description="How often processing jobs the broker refused are resent",
    )
    DISPATCH_STALL_SECONDS: int = Field(
        default=1800,
        ge=60,
        description="How long a sent job may wait for a worker before it is resent",
    )
    DRAFT_REVOCATION_RETRY_SECONDS: int = Field(
        default=300,
        ge=30,
        description="How often failed Google Docs permission revocations are retried",
    )
    RATE_LIMIT_ENABLED: bool = Field(
        default=True, description="Throttle sign-up, sign-in and verification resends"
    )
    RATE_LIMIT_STORAGE_URL: str = Field(
        default="memory://",
        description="Where rate-limit counters live, e.g. redis://redis:6379/1",
    )
    # Browser origins allowed to call the API (the front-end). Comma-separated
    # in .env, e.g. CORS_ORIGINS=http://localhost:3000,https://app.example.com
    CORS_ORIGINS: str = Field(
        default="http://localhost:3000,http://localhost:5173",
        description="Comma-separated front-end origins allowed by CORS",
    )
    GOOGLE_CONNECTED_REDIRECT: str = Field(
        default="http://localhost:3000/settings",
        description="Frontend page the lawyer lands on after connecting Google",
    )
    # Must also be on Supabase's Redirect URLs allow-list, and the Reset
    # Password email template must link to
    # {{ .RedirectTo }}?token_hash={{ .TokenHash }}&type=recovery.
    PASSWORD_RESET_REDIRECT: str = Field(
        default="http://localhost:3000/reset-password",
        description="Frontend page a password-recovery email links to",
    )
    # Invitation emails link here. Must be http(s): mail clients such as
    # Gmail strip links with a custom scheme (lexchain://), leaving plain text.
    SIGN_UP_URL: str = Field(
        default="https://lexchain.me/register",
        description="Frontend sign-up page that invitation emails link to",
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"  # ignore unused vars


settings = Settings()  # type: ignore
