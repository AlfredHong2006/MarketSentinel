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
