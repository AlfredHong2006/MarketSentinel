"""Typed domain objects shared across pipeline stages and API responses."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MarketName = Literal["S&P 500", "FTSE 100"]
HealthStatus = Literal["healthy", "degraded", "unavailable"]


class Constituent(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    yahoo_symbol: str
    name: str
    market: MarketName
    aliases: tuple[str, ...] = ()


class UniverseResult(BaseModel):
    constituents: list[Constituent]
    source: str
    is_fallback: bool
    fetched_at: datetime
    message: str | None = None


class Article(BaseModel):
    model_config = ConfigDict(frozen=True)

    fingerprint: str
    ticker: str
    title: str
    url: str
    source: str
    published_at: datetime
    fetched_at: datetime
    provider: str
    relevance_score: float = Field(ge=0, le=1)
    is_demo: bool = False


class ScoredArticle(Article):
    label: Literal["positive", "negative", "neutral"]
    positive: float = Field(ge=0, le=1)
    negative: float = Field(ge=0, le=1)
    neutral: float = Field(ge=0, le=1)
    sentiment_score: float = Field(ge=-1, le=1)
    model_name: str
    scored_at: datetime


class SourceHealth(BaseModel):
    provider: str
    status: HealthStatus
    records_received: int = 0
    valid_records: int = 0
    latency_ms: int | None = None
    message: str | None = None


class NewsFetchResult(BaseModel):
    articles: list[Article]
    health: SourceHealth


class DailySentiment(BaseModel):
    ticker: str
    date: date
    score: float = Field(ge=-1, le=1)
    moving_average_7d: float = Field(ge=-1, le=1)
    article_count: int = Field(ge=0)
    computed_at: datetime


class PricePoint(BaseModel):
    date: date
    close: float
    volume: float


class PriceHistory(BaseModel):
    symbol: str
    points: list[PricePoint]
    source: str = "Yahoo Finance via yfinance"
    fetched_at: datetime


class ForecastMetrics(BaseModel):
    validation_accuracy: float = Field(ge=0, le=1)
    majority_baseline_accuracy: float = Field(ge=0, le=1)
    momentum_baseline_accuracy: float = Field(ge=0, le=1)
    roc_auc: float | None = Field(default=None, ge=0, le=1)
    validation_samples: int = Field(ge=0)


class ForecastResult(BaseModel):
    horizon_trading_days: int = 5
    probability_up: float = Field(ge=0, le=1)
    as_of: date
    trained_through: date
    training_samples: int
    sentiment_coverage: float = Field(ge=0, le=1)
    sentiment_features_used: bool
    features: list[str]
    metrics: ForecastMetrics
    model: str = "logistic regression"
    warning: str


class AnalysisResult(BaseModel):
    constituent: Constituent
    price_history: PriceHistory
    articles: list[ScoredArticle]
    daily_sentiment: list[DailySentiment]
    forecast: ForecastResult
    source_health: list[SourceHealth]
    generated_at: datetime
    disclaimer: str
