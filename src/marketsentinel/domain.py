"""Typed domain objects shared across pipeline stages and API responses."""

from datetime import date, datetime
from enum import StrEnum
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
    provider_article_id: str | None = None
    published_at: datetime
    fetched_at: datetime
    provider: str
    relevance_score: float = Field(ge=0, le=1)
    # RSS descriptions are optional and deliberately bounded: the application does not scrape or
    # retain full publisher article bodies for event analysis.
    snippet: str | None = Field(default=None, max_length=4_000)
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
    funnel: "IngestionFunnel" = Field(default_factory=lambda: IngestionFunnel())


class IngestionFunnel(BaseModel):
    """Non-sensitive, stage-specific counts for one ingestion request."""

    retrieved: int = Field(default=0, ge=0)
    invalid_dates: int = Field(default=0, ge=0)
    irrelevant: int = Field(default=0, ge=0)
    invalid_urls: int = Field(default=0, ge=0)
    exact_duplicates: int = Field(default=0, ge=0)
    canonical_url_duplicates: int = Field(default=0, ge=0)
    near_title_duplicates: int = Field(default=0, ge=0)
    database_conflicts: int = Field(default=0, ge=0)
    request_limited: int = Field(default=0, ge=0)
    relevant: int = Field(default=0, ge=0)
    unique: int = Field(default=0, ge=0)
    scored: int = Field(default=0, ge=0)
    previously_scored: int = Field(default=0, ge=0)
    provider_failures: int = Field(default=0, ge=0)

    def merged(self, other: "IngestionFunnel") -> "IngestionFunnel":
        """Add provider-stage counts while leaving final service-stage counts to the caller."""

        return IngestionFunnel(
            retrieved=self.retrieved + other.retrieved,
            invalid_dates=self.invalid_dates + other.invalid_dates,
            irrelevant=self.irrelevant + other.irrelevant,
            invalid_urls=self.invalid_urls + other.invalid_urls,
            exact_duplicates=self.exact_duplicates + other.exact_duplicates,
            canonical_url_duplicates=self.canonical_url_duplicates + other.canonical_url_duplicates,
            near_title_duplicates=self.near_title_duplicates + other.near_title_duplicates,
            database_conflicts=self.database_conflicts + other.database_conflicts,
            request_limited=self.request_limited + other.request_limited,
            relevant=self.relevant + other.relevant,
            unique=self.unique + other.unique,
            scored=self.scored + other.scored,
            previously_scored=self.previously_scored + other.previously_scored,
            provider_failures=self.provider_failures + other.provider_failures,
        )


class DailySentiment(BaseModel):
    ticker: str
    date: date
    score: float = Field(ge=-1, le=1)
    moving_average_7d: float = Field(ge=-1, le=1)
    trend_3: float = Field(default=0, ge=-1, le=1)
    article_count: int = Field(ge=0)
    positive_share: float = Field(default=0, ge=0, le=1)
    negative_share: float = Field(default=0, ge=0, le=1)
    weighted_disagreement: float = Field(default=0, ge=0, le=1)
    aggregate_weight: float = Field(default=0, ge=0)
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


class AnalyzedEvent(BaseModel):
    """Compact product-facing view of one genuine stored article analysis."""

    article_id: str
    event_date: date
    article_url: str
    event_type: str
    summary: str
    direction: str
    magnitude: float = Field(ge=0, le=1)
    extraction_confidence: float = Field(ge=0, le=1)
    evidence_strength: float = Field(ge=0, le=1)


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
    analyzed_events: list[AnalyzedEvent] = Field(default_factory=list)
    intelligence_events: list["CompanyIntelligenceEvent"] = Field(default_factory=list)
    forecast: ForecastResult
    source_health: list[SourceHealth]
    ingestion_funnel: IngestionFunnel
    generated_at: datetime
    disclaimer: str


class EventType(StrEnum):
    EARNINGS = "earnings"
    PRODUCT_LAUNCH = "product_launch"
    INVESTMENT = "investment"
    ACQUISITION = "acquisition"
    REGULATION = "regulation"
    LITIGATION = "litigation"
    SUPPLY_DISRUPTION = "supply_disruption"
    MANAGEMENT_CHANGE = "management_change"
    FINANCING = "financing"
    MACROECONOMIC_EXPOSURE = "macroeconomic_exposure"
    PARTNERSHIP = "partnership"
    CONTRACT_AWARD = "contract_award"
    CONTRACT_LOSS = "contract_loss"
    ANALYST_OR_GUIDANCE_CHANGE = "analyst_or_guidance_change"
    OTHER = "other"
    UNCERTAIN = "uncertain"


class EventDirection(StrEnum):
    """Possible business direction, not a prediction of a security's price."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    NEUTRAL = "neutral"
    UNCERTAIN = "uncertain"


class TimeHorizon(StrEnum):
    IMMEDIATE = "immediate"
    DAYS = "days"
    WEEKS = "weeks"
    MONTHS = "months"
    LONG_TERM = "long_term"
    UNCERTAIN = "uncertain"


class EvidenceStatus(StrEnum):
    CORROBORATED = "corroborated"
    CONTRADICTED = "contradicted"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"


class SourceClass(StrEnum):
    OFFICIAL_COMPANY = "official_company"
    REGULATORY_OR_FILING = "regulatory_or_filing"
    MAJOR_FINANCIAL_NEWS = "major_financial_news"
    INDUSTRY_SPECIALIST = "industry_specialist"
    GENERAL_NEWS = "general_news"
    COMMENTARY_OR_OPINION = "commentary_or_opinion"
    UNKNOWN = "unknown"


class CompanyReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    symbol: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)


class ArticleEvidenceReference(BaseModel):
    """A compact pointer to an item already stored by MarketSentinel."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    article_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    publisher: str = Field(min_length=1, max_length=200)
    published_at: datetime
    url: str = Field(min_length=1, max_length=2_000)


class EventExtraction(BaseModel):
    """Stage A: a bounded extraction from one stored article; identity stays application-owned."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_type: EventType
    summary: str = Field(min_length=1, max_length=2_000)
    direction: EventDirection
    magnitude: float = Field(ge=0, le=1, multiple_of=0.05)
    time_horizon: TimeHorizon
    model_confidence: float = Field(ge=0, le=1, multiple_of=0.05)
    important_claims: list[str] = Field(default_factory=list, max_length=8)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)
    positive_channels: list[str] = Field(default_factory=list, max_length=8)
    negative_channels: list[str] = Field(default_factory=list, max_length=8)


class ClaimAssessment(BaseModel):
    """Stage B: evidence assessment for one application-assigned claim identifier."""

    model_config = ConfigDict(extra="forbid", strict=True)

    claim_id: str = Field(min_length=1, max_length=32)
    status: EvidenceStatus
    reasoning: str = Field(min_length=1, max_length=1_000)
    evidence_article_ids: list[str] = Field(default_factory=list, max_length=6)
    confidence: float = Field(ge=0, le=1, multiple_of=0.05)


class ClaimAssessments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    assessments: list[ClaimAssessment] = Field(default_factory=list, max_length=8)


class RelatedCompanyProposal(BaseModel):
    """Stage C proposal. Tickers are normalised against an application-supplied candidate set."""

    model_config = ConfigDict(extra="forbid", strict=True)

    ticker: str = Field(min_length=1, max_length=20)
    relationship_context: str = Field(min_length=1, max_length=500)
    possible_effect_direction: EventDirection
    reasoning: str = Field(min_length=1, max_length=1_000)
    confidence: float = Field(ge=0, le=1, multiple_of=0.05)


class RelatedCompanyProposals(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    related_companies: list[RelatedCompanyProposal] = Field(default_factory=list, max_length=10)


class RelatedCompanyAnalysis(RelatedCompanyProposal):
    """A persisted Stage C result whose company name comes from the supported universe."""

    company: CompanyReference


class ArticleAnalysis(BaseModel):
    """Persisted, versioned three-stage article intelligence; never a price prediction."""

    model_config = ConfigDict(extra="forbid", strict=True)

    article_id: str = Field(min_length=1, max_length=128)
    source_reference: ArticleEvidenceReference
    source_class: SourceClass
    subject_company: CompanyReference
    event: EventExtraction
    claims: list[ClaimAssessment] = Field(default_factory=list, max_length=8)
    related_companies: list[RelatedCompanyAnalysis] = Field(default_factory=list, max_length=10)
    evidence_count: int = Field(ge=0, le=8)
    evidence_strength: float = Field(ge=0, le=1)
    evidence_sources: list[ArticleEvidenceReference] = Field(default_factory=list, max_length=8)
    evidence_fingerprint: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=200)
    stage_a_prompt_version: str = Field(min_length=1, max_length=100)
    stage_b_prompt_version: str = Field(min_length=1, max_length=100)
    stage_c_prompt_version: str = Field(min_length=1, max_length=100)
    schema_version: str = Field(min_length=1, max_length=100)
    analysis_created_at: datetime

    def to_analyzed_event(self) -> AnalyzedEvent:
        """Project one compatible stored analysis into the dashboard marker contract."""

        return AnalyzedEvent(
            article_id=self.article_id,
            event_date=self.source_reference.published_at.date(),
            article_url=self.source_reference.url,
            event_type=self.event.event_type,
            summary=self.event.summary,
            direction=self.event.direction,
            magnitude=self.event.magnitude,
            extraction_confidence=self.event.model_confidence,
            evidence_strength=self.evidence_strength,
        )

    def to_company_intelligence_event(self) -> "CompanyIntelligenceEvent":
        """Project compatible stored analysis into the product intelligence contract."""

        return CompanyIntelligenceEvent(
            article_id=self.article_id,
            source_reference=self.source_reference,
            source_class=self.source_class,
            subject_company=self.subject_company,
            event=self.event,
            claims=self.claims,
            related_companies=self.related_companies,
            evidence_strength=self.evidence_strength,
            evidence_sources=self.evidence_sources,
        )


class CompanyIntelligenceEvent(BaseModel):
    """Product-facing stored event detail for the dashboard's intelligence section."""

    model_config = ConfigDict(extra="forbid", strict=True)

    article_id: str = Field(min_length=1, max_length=128)
    source_reference: ArticleEvidenceReference
    source_class: SourceClass
    subject_company: CompanyReference
    event: EventExtraction
    claims: list[ClaimAssessment] = Field(default_factory=list, max_length=8)
    related_companies: list[RelatedCompanyAnalysis] = Field(default_factory=list, max_length=10)
    evidence_strength: float = Field(ge=0, le=1)
    evidence_sources: list[ArticleEvidenceReference] = Field(default_factory=list, max_length=8)


class ArticleAnalysisResponse(BaseModel):
    """Safe on-demand API result; a failure never substitutes a made-up analysis."""

    article_id: str
    status: Literal["cached", "generated", "unavailable", "failed", "not_found"]
    analysis: ArticleAnalysis | None = None
    message: str | None = None


AnalysisResult.model_rebuild()
