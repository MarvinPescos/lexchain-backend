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
    SBERT_MODEL: str = "all-MiniLM-L6-v2"

    # OCR
    USE_REAL_OCR: bool = Field(default=False, description="Use real OCR providers")
    OCR_ENGINE: str = Field(default="pypdf", description="pypdf | tesseract")

    CHUNKER_ENGINE: str = Field(..., description="Engine would be used in chunking")

    # MinerU
    MINERU_MODE: str = Field(default="api", description="MinerU mode: api")
    MINERU_API_URL: str = Field(
        default="http://localhost:8888", description="MinerU API URL"
    )
    MINERU_BACKEND: str = Field(
        default="pipeline", description="MinerU backend: pipeline | vlm-engine"
    )
    MINERU_TIMEOUT: int = Field(
        default=120, description="MinerU API timeout in seconds"
    )

    WEB_RPC_URL: str = Field(..., description="")
    CONTRACT_ADDRESS: str = Field(..., description="")
    CONTRACT_OWNER_PRIVATE_KEY: str = Field(..., description="")

    RESEND_API_KEY: str = Field(..., min_length=1, description="Resend API key")
    RESEND_FROM_EMAIL: str = Field(
        default="noreply@lexchain.app", description="Sender address"
    )

    CELERY_BROKER_URL: str = Field(..., description="Broker URL")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"  # ignore unused vars


settings = Settings()  # type: ignore
