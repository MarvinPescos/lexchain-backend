from pydantic_settings import BaseSettings
from pydantic import Field, validator
from typing import List
import re

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
    DB_MAX_OVERFLOW: int = Field(default=0, description="Max overflow connections beyond pool_size")
    DB_POOL_PRE_PING: bool = Field(default=False, description="Verify connections before checkout")
    DB_POOL_RECYCLE: int = Field(default=-1, description="Recycle connections after N seconds (-1 = never)")
    DB_ECHO: bool = Field(default=False, description="Echo SQL statements to log")

    # Application Settings
    ENVIRONMENT: str = Field(..., description="development or production")

    # Supabase Config
    SUPABASE_URL: str = Field(..., description="Supabase project URL")
    SUPABASE_ANON_KEY: str = Field(..., description="Supabase anonymous key") 
    SUPABASE_SERVICE_ROLE_KEY: str = Field(..., description="Supabase role key for admin operations")
    # SUPABASE_JWT_SECRET: str = Field(..., description="Supabase JWT secret for token verification") #Not been used I think

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra= "ignore" #ignore unused vars
    
settings = Settings()