from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from app.services.ai.base_recognition_service import RecognitionResult


class PricingProviderError(Exception):
    """Base exception for pricing provider failures."""


class PricingProviderUnavailableError(PricingProviderError):
    """Raised when a pricing provider is configured but not ready."""


class PricingProviderTimeoutError(PricingProviderError):
    """Raised when a pricing provider times out."""


class PricingProviderRateLimitError(PricingProviderError):
    """Raised when a pricing provider is rate limited."""


class EmptyMarketDataError(PricingProviderError):
    """Raised when a provider returns no usable market data."""


@dataclass(frozen=True)
class MarketComparableSale:
    source: str
    title: str
    soldPrice: int
    currency: str
    soldDate: str
    condition: str
    url: str | None = None


@dataclass(frozen=True)
class PricingResult:
    estimatedMarketValue: int
    lowEstimate: int
    highEstimate: int
    currency: str
    pricingSource: str
    pricingConfidence: int
    lastUpdated: str
    marketTrend: str = "Stable"
    sourceCount: int = 1
    pricingAge: str = "fresh"
    comparableSales: list[MarketComparableSale] = field(default_factory=list)
    fallbackUsed: bool = False
    cacheStatus: str = "miss"
    providerDiagnostics: dict[str, str] = field(default_factory=dict)


class PricingProvider(Protocol):
    provider_name: str

    def price(self, recognition: RecognitionResult) -> PricingResult:
        """Estimate market pricing for a recognized collectible."""
        ...


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
