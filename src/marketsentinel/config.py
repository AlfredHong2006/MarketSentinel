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

    # Public deployment mode. Off by default, so a local/private run keeps every existing
    # capability. When on, the API refuses the two spending endpoints (POST /analyze and
    # POST /articles/analyze) -- enforced at the API boundary, never in the client, because the
    # API is reachable independently of any frontend. Search and every read endpoint stay open
    # across the full constituent universe in both modes; a company with nothing stored reads as
    # an honest empty state rather than being hidden.
    #
    # public_prepared_companies labels the deliberately backfilled deep demonstrations. It is an
    # explicit editorial list, NOT an allowlist and NOT a computed quality threshold: which
    # companies count as prepared is a product decision, not something the application should
    # infer from its own stored counts.
    public_mode: bool = False
    public_prepared_companies: tuple[str, ...] = ("NVDA", "PFE")
    public_default_symbol: str = "NVDA"

    # A public page fetches prices on every visitor page load, and price history is not persisted.
    # This is a per-process in-memory TTL only: no database column, no migration, no external
    # cache. It bounds third-party calls per company to roughly one per TTL per process.
    price_cache_ttl_seconds: float = Field(default=900.0, ge=0, le=86_400)

    # Browser origins allowed to call the API. The Streamlit dashboard's port is joined by the
    # Vite dev and preview ports so a future React client needs no code change to talk to a local
    # API. Configurable rather than hardcoded because a deployed client is served from elsewhere.
    # Override with a JSON list, e.g. MARKETSENTINEL_CORS_ALLOW_ORIGINS='["https://example.com"]'.
    cors_allow_origins: tuple[str, ...] = (
        "http://localhost:8501",
        "http://127.0.0.1:8501",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    )

    finbert_model: str = "ProsusAI/finbert"
    finbert_device: Literal["auto", "cpu", "cuda"] = "auto"
    finbert_batch_size: int = Field(default=8, ge=1, le=64)
    hf_token: str | None = None

    # Optional OpenAI-compatible structured-output provider. The app remains usable without it.
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    article_analysis_evidence_limit: int = Field(default=5, ge=0, le=8)
    analysis_auto_candidates: int = Field(default=15, ge=0, le=40)
    analysis_auto_max_new_per_run: int = Field(default=6, ge=0, le=15)

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
