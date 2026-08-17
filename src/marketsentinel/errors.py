"""Application-specific exceptions translated at the API boundary."""


class MarketSentinelError(Exception):
    """Base class for expected application failures."""


class ConstituentNotFoundError(MarketSentinelError):
    """Raised when a ticker or company cannot be resolved."""


class ProviderError(MarketSentinelError):
    """Raised when an external data provider cannot return valid data."""


class SentimentModelError(MarketSentinelError):
    """Raised when FinBERT cannot be loaded or used."""


class ForecastError(MarketSentinelError):
    """Raised when there is not enough valid history for a forecast."""


class ArticleAnalysisUnavailableError(MarketSentinelError):
    """Raised when on-demand event analysis has not been configured."""


class ArticleAnalysisProviderError(MarketSentinelError):
    """Raised when a configured analysis provider times out or fails safely."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("The article-analysis provider did not return a usable response.")


class ArticleAnalysisValidationError(MarketSentinelError):
    """Base class for safe structural and semantic article-analysis rejections."""


class ArticleAnalysisStructuralValidationError(ArticleAnalysisValidationError):
    """Raised when provider JSON does not conform to the local Pydantic draft schema."""

    category = "pydantic_validation"


class ArticleAnalysisSemanticValidationError(ArticleAnalysisValidationError):
    """Raised when structurally valid output conflicts with supplied application evidence."""

    category = "semantic_validation"
