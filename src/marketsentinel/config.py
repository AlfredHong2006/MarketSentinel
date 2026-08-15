"""Application configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings with safe local defaults."""

    model_config = SettingsConfigDict(
        env_prefix="MARKETSENTINEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_path: Path = Path("data/marketsentinel.db")
    constituent_cache_path: Path = Path("data/constituents_cache.json")
    demo_news_path: Path = Path("data/demo/articles.json")
    api_base_url: str = "http://127.0.0.1:8000"

    finbert_model: str = "ProsusAI/finbert"
    finbert_device: Literal["auto", "cpu", "cuda"] = "auto"
    finbert_batch_size: int = Field(default=8, ge=1, le=64)
    hf_token: str | None = None

    news_lookback_days: int = Field(default=7, ge=1, le=30)
    news_max_articles: int = Field(default=50, ge=1, le=200)
    historical_news_days: int = Field(default=30, ge=1, le=30)
    historical_news_max_articles: int = Field(default=180, ge=1, le=500)
    historical_gdelt_window_days: int = Field(default=30, ge=1, le=30)
    historical_gdelt_request_interval_seconds: float = Field(default=5.25, ge=5, le=30)
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    sentiment_half_life_hours: float = Field(default=24.0, gt=0)
    allow_demo_fallback: bool = True

    user_agent: str = "MarketSentinel/0.1 (portfolio research application)"


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings object per process."""

    return Settings()
