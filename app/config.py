import os
from typing import Literal, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    # GitHub Integration
    GITHUB_TOKEN: Optional[str] = None
    GITHUB_WEBHOOK_SECRET: Optional[str] = None
    GITHUB_API_BASE: str = "https://api.github.com"

    # LLM Settings
    LLM_PROVIDER: Literal["gemini", "openai", "mock"] = "mock"
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-2.5-flash"
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_BASE_URL: Optional[str] = None

    # Database
    DATABASE_URL: str = "sqlite:///./pr_reviewer.db"

    # Review Guardrails & Limits
    MAX_DIFF_FILES: int = 40
    MAX_DIFF_BYTES: int = 600_000
    MAX_FILE_BYTES: int = 150_000
    ENABLE_RUFF: bool = True
    AUTO_POST_TO_GITHUB: bool = False


settings = Settings()

