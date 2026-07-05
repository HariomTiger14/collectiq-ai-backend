from app.core.config import settings
from app.services.pricing.base_pricing_provider import (
    PricingProvider,
    PricingProviderUnavailableError,
)
from app.services.pricing.aggregation_service import PricingAggregationService
from app.services.pricing.ebay_pricing_provider import EbayPricingProvider
from app.services.pricing.mock_pricing_provider import MockPricingProvider
from app.services.pricing.pricecharting_pricing_provider import (
    PriceChartingPricingProvider,
)
from app.services.pricing.tcgplayer_pricing_provider import TCGPlayerPricingProvider


_mock_provider = MockPricingProvider()
_ebay_provider = EbayPricingProvider(
    access_token=settings.ebay_access_token,
    browse_api_url=settings.ebay_browse_api_url,
    marketplace_id=settings.ebay_marketplace_id,
    timeout_seconds=settings.ebay_timeout_seconds,
    cache_ttl_seconds=settings.pricing_cache_ttl_seconds,
    min_interval_ms=settings.pricing_provider_min_interval_ms,
)
_tcgplayer_provider = TCGPlayerPricingProvider(
    client_id=settings.tcgplayer_client_id,
    client_secret=settings.tcgplayer_client_secret,
    api_base=settings.tcgplayer_api_base,
    timeout_seconds=settings.tcgplayer_timeout_seconds,
    cache_ttl_seconds=settings.pricing_cache_ttl_seconds,
    min_interval_ms=settings.pricing_provider_min_interval_ms,
)
_pricecharting_provider = PriceChartingPricingProvider(
    api_key=settings.pricecharting_api_key,
    api_base=settings.pricecharting_api_base,
    timeout_seconds=settings.pricecharting_timeout_seconds,
    cache_ttl_seconds=settings.pricing_cache_ttl_seconds,
    min_interval_ms=settings.pricing_provider_min_interval_ms,
)


def get_pricing_provider(provider_name: str | None = None) -> PricingProvider:
    selected_provider = (provider_name or settings.pricing_provider).strip().lower()

    if selected_provider == "mock":
        return PricingAggregationService([_mock_provider], fallback_provider=_mock_provider)

    if selected_provider == "ebay":
        return PricingAggregationService([_ebay_provider], fallback_provider=_mock_provider)

    if selected_provider == "tcgplayer":
        return PricingAggregationService([_tcgplayer_provider], fallback_provider=_mock_provider)

    if selected_provider == "pricecharting":
        return PricingAggregationService([_pricecharting_provider], fallback_provider=_mock_provider)

    if selected_provider == "aggregate":
        return PricingAggregationService(
            [_ebay_provider, _tcgplayer_provider, _pricecharting_provider],
            fallback_provider=_mock_provider,
        )

    raise PricingProviderUnavailableError(
        f"Unsupported PRICING_PROVIDER '{selected_provider}'. "
        "Supported providers: mock, ebay, tcgplayer, pricecharting, aggregate."
    )
