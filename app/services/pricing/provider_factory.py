from app.core.config import settings
from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.base_pricing_provider import (
    PricingProvider,
    PricingProviderUnavailableError,
)
from app.services.pricing.aggregation_service import PricingAggregationService
from app.services.pricing.cache import (
    ChainedProviderThrottle,
    ProviderThrottle,
    SharedProviderThrottle,
)
from app.services.pricing.ebay_pricing_provider import EbayPricingProvider
from app.services.pricing.kicksdb_catalog_matcher import KicksDBCatalogMatcher
from app.services.pricing.kicksdb_pricing_provider import KicksDBPricingProvider
from app.services.pricing.mock_pricing_provider import MockPricingProvider
from app.services.pricing.pricecharting_pricing_provider import (
    PriceChartingPricingProvider,
)
from app.services.pricing.tcgplayer_pricing_provider import TCGPlayerPricingProvider


_mock_provider = MockPricingProvider()
_ebay_provider = EbayPricingProvider(
    access_token=settings.ebay_access_token,
    client_id=settings.ebay_client_id,
    client_secret=settings.ebay_client_secret,
    oauth_token_url=settings.ebay_oauth_token_url,
    oauth_scope=settings.ebay_oauth_scope,
    marketplace_insights_api_url=settings.ebay_marketplace_insights_api_url,
    partner_access_granted=settings.ebay_partner_access_granted,
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
_kicksdb_catalog_matcher = KicksDBCatalogMatcher(
    supabase_url=settings.supabase_url,
    service_role_key=settings.supabase_service_role_key,
    timeout_seconds=settings.kicksdb_timeout_seconds,
)
_kicksdb_provider = KicksDBPricingProvider(
    api_key=settings.kicksdb_api_key,
    api_base=settings.kicksdb_api_base,
    timeout_seconds=settings.kicksdb_timeout_seconds,
    cache_ttl_seconds=settings.pricing_cache_ttl_seconds,
    min_interval_ms=settings.pricing_provider_min_interval_ms,
    catalog_matcher=_kicksdb_catalog_matcher,
)
_pricecharting_provider = PriceChartingPricingProvider(
    api_key=settings.pricecharting_api_key,
    api_base=settings.pricecharting_api_base,
    timeout_seconds=settings.pricecharting_timeout_seconds,
    cache_ttl_seconds=settings.pricing_cache_ttl_seconds,
    min_interval_ms=settings.pricecharting_provider_min_interval_ms,
    throttle=ChainedProviderThrottle(
        ProviderThrottle(settings.pricecharting_provider_min_interval_ms),
        SharedProviderThrottle(settings.pricecharting_provider_min_interval_ms)
        if settings.pricecharting_shared_throttle_enabled
        else None,
    ),
)


class AutoPricingProvider(PricingProvider):
    provider_name = "auto"

    def price(self, recognition: RecognitionResult):
        providers = _providers_for_recognition(recognition)
        if not providers:
            raise PricingProviderUnavailableError(
                "No real pricing provider is configured for this collectible. "
                "Set PRICECHARTING_API_KEY, KICKSDB_API_KEY, "
                "TCGPLAYER_CLIENT_ID/TCGPLAYER_CLIENT_SECRET, or an approved "
                "eBay sold-comps endpoint."
            )
        return PricingAggregationService(providers).price(recognition)


_auto_provider = AutoPricingProvider()


def get_pricing_provider(provider_name: str | None = None) -> PricingProvider:
    selected_provider = (provider_name or settings.pricing_provider).strip().lower()

    if selected_provider in {"auto", "real"}:
        return _auto_provider

    if selected_provider == "mock":
        return PricingAggregationService([_mock_provider], fallback_provider=_mock_provider)

    if selected_provider == "ebay":
        return _ebay_provider

    if selected_provider == "tcgplayer":
        return _tcgplayer_provider

    if selected_provider == "kicksdb":
        return _kicksdb_provider

    if selected_provider == "pricecharting":
        return _pricecharting_provider

    if selected_provider == "aggregate":
        providers = _configured_providers(
            [_ebay_provider, _tcgplayer_provider, _kicksdb_provider, _pricecharting_provider]
        )
        if not providers:
            raise PricingProviderUnavailableError(
                "PRICING_PROVIDER=aggregate requires at least one configured "
                "real pricing provider."
            )
        return PricingAggregationService(providers)

    raise PricingProviderUnavailableError(
        f"Unsupported PRICING_PROVIDER '{selected_provider}'. "
        "Supported providers: auto, mock, ebay, tcgplayer, kicksdb, pricecharting, aggregate."
    )


def _providers_for_recognition(recognition: RecognitionResult) -> list[PricingProvider]:
    text = _recognition_text(recognition)
    preferred: list[PricingProvider] = []

    if _looks_like_sneaker_or_streetwear(text):
        preferred.extend([_kicksdb_provider])
    elif _looks_like_trading_card(text):
        preferred.extend([_tcgplayer_provider, _pricecharting_provider, _ebay_provider])
    elif _looks_like_pricecharting_direct_category(text):
        preferred.extend([_pricecharting_provider, _ebay_provider])
    else:
        preferred.extend([_ebay_provider, _pricecharting_provider])

    return _configured_providers(preferred)


def _configured_providers(providers: list[PricingProvider]) -> list[PricingProvider]:
    configured: list[PricingProvider] = []
    for provider in providers:
        if provider.provider_name == "ebay" and _ebay_sold_comps_configured(provider):
            configured.append(provider)
        elif (
            provider.provider_name == "tcgplayer"
            and _provider_value(provider, "_client_id")
            and _provider_value(provider, "_client_secret")
        ):
            configured.append(provider)
        elif (
            provider.provider_name == "kicksdb"
            and _provider_value(provider, "_api_key")
        ):
            configured.append(provider)
        elif (
            provider.provider_name == "pricecharting"
            and _provider_value(provider, "_api_key")
        ):
            configured.append(provider)
    seen: set[str] = set()
    unique: list[PricingProvider] = []
    for provider in configured:
        if provider.provider_name in seen:
            continue
        seen.add(provider.provider_name)
        unique.append(provider)
    return unique


def _ebay_sold_comps_configured(provider: PricingProvider) -> bool:
    has_credentials = bool(
        _provider_value(provider, "_access_token")
        or (
            _provider_value(provider, "_client_id")
            and _provider_value(provider, "_client_secret")
        )
    )
    has_partner_access = bool(getattr(provider, "_partner_access_granted", False))
    return has_partner_access and has_credentials and bool(
        _provider_value(provider, "_marketplace_insights_api_url")
    )


def _provider_value(provider: PricingProvider, attribute: str) -> str:
    return str(getattr(provider, attribute, "") or "").strip()


def _recognition_text(recognition: RecognitionResult) -> str:
    values = [
        recognition.title,
        recognition.category,
        recognition.brand,
        recognition.setName,
        recognition.series,
        recognition.cardNumber,
        recognition.playerOrCharacter,
        recognition.rarity,
        recognition.edition,
        recognition.notes,
    ]
    return " ".join(str(value).lower() for value in values if value)


def _looks_like_trading_card(text: str) -> bool:
    keywords = {
        "trading card",
        "pokemon",
        "pokémon",
        "magic",
        "mtg",
        "yugioh",
        "yu-gi-oh",
        "sports card",
        "rookie card",
        "card number",
        "holo",
        "foil",
        "psa",
        "bgs",
        "cgc",
    }
    return any(keyword in text for keyword in keywords)


def _looks_like_sneaker_or_streetwear(text: str) -> bool:
    keywords = {
        "sneaker",
        "sneakers",
        "streetwear",
        "shoe",
        "shoes",
        "trainer",
        "trainers",
        "nike",
        "air jordan",
        "jordan",
        "adidas",
        "yeezy",
        "new balance",
        "asics",
        "puma",
        "supreme",
        "off-white",
        "stockx",
        "goat",
    }
    return any(keyword in text for keyword in keywords)


def _looks_like_pricecharting_direct_category(text: str) -> bool:
    keywords = {
        "video game",
        "game cartridge",
        "nintendo",
        "playstation",
        "xbox",
        "comic",
        "comic book",
        "manga",
        "lego",
        "building set",
        "funko",
        "funko pop",
        "vinyl figure",
        "coin",
        "numismatic",
        "penny",
        "cent",
    }
    return any(keyword in text for keyword in keywords)
