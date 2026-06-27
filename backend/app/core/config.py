from pydantic import Field
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

    # LLM
    GROQ_API_KEY: str = Field(..., description="Groq api key")
    NVIDIA_API_KEY: str = Field(..., description="nvidia api key")
    USE_REAL_LLM: bool = Field(default=True, description="Groq api key")

    # Embedder
    USE_REAL_EMBEDDER: bool = Field(default=False, description="Basta Embedder")
    SBERT_MODEL: str = "all-MiniLM-L6-v2"

    # OCR
    USE_REAL_OCR: bool = Field(default=False, description="Use real OCR providers")
    OCR_ENGINE: str = Field(default="pypdf", description="pypdf | tesseract")

    # MinerU
    MINERU_MODE: str = Field(default="api", description="MinerU mode: api")
    MINERU_API_URL: str = Field(default="http://localhost:8888", description="MinerU API URL")
    MINERU_BACKEND: str = Field(default="pipeline", description="MinerU backend: pipeline | vlm-engine")
    MINERU_TIMEOUT: int = Field(default=120, description="MinerU API timeout in seconds")

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
