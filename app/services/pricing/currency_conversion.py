from __future__ import annotations

from dataclasses import replace

from app.core.config import settings
from app.services.pricing.base_pricing_provider import MarketComparableSale, PricingResult


SUPPORTED_DISPLAY_CURRENCIES = {"AUD", "CAD", "GBP", "USD"}


def normalize_display_currency(value: str | None) -> str:
    currency = (value or settings.default_display_currency or "AUD").strip().upper()
    if currency in SUPPORTED_DISPLAY_CURRENCIES:
        return currency
    return "AUD"


def convert_pricing_result(
    pricing: PricingResult,
    *,
    target_currency: str,
) -> PricingResult:
    display_currency = normalize_display_currency(target_currency)
    source_currency = (pricing.currency or display_currency).strip().upper()
    if pricing.valuationStatus != "market_estimated":
        return replace(
            pricing,
            currency=display_currency,
            originalCurrency=source_currency,
            exchangeRateUsed=_exchange_rate(source_currency, display_currency),
            exchangeRateDate=pricing.lastUpdated,
        )

    rate = _exchange_rate(source_currency, display_currency)
    original_value = pricing.originalMarketValue or pricing.estimatedMarketValue
    original_low = pricing.originalLowEstimate or pricing.lowEstimate
    original_high = pricing.originalHighEstimate or pricing.highEstimate
    original_currency = pricing.originalCurrency or source_currency

    return replace(
        pricing,
        estimatedMarketValue=_convert_amount(pricing.estimatedMarketValue, rate),
        lowEstimate=_convert_amount(pricing.lowEstimate, rate),
        highEstimate=_convert_amount(pricing.highEstimate, rate),
        currency=display_currency,
        comparableSales=[
            _convert_sale(sale, target_currency=display_currency)
            for sale in pricing.comparableSales
        ],
        originalMarketValue=original_value,
        originalLowEstimate=original_low,
        originalHighEstimate=original_high,
        originalCurrency=original_currency,
        exchangeRateUsed=rate,
        exchangeRateDate=pricing.lastUpdated,
    )


def _convert_sale(
    sale: MarketComparableSale,
    *,
    target_currency: str,
) -> MarketComparableSale:
    source_currency = (sale.currency or target_currency).strip().upper()
    rate = _exchange_rate(source_currency, target_currency)
    return replace(
        sale,
        soldPrice=_convert_amount(sale.soldPrice, rate),
        currency=target_currency,
    )


def _convert_amount(value: int, rate: float) -> int:
    if value <= 0:
        return 0
    return max(1, round(value * rate))


def _exchange_rate(source_currency: str, target_currency: str) -> float:
    source = normalize_display_currency(source_currency)
    target = normalize_display_currency(target_currency)
    if source == target:
        return 1
    usd_to_target = _usd_rate(target)
    usd_to_source = _usd_rate(source)
    return usd_to_target / usd_to_source


def _usd_rate(currency: str) -> float:
    normalized = normalize_display_currency(currency)
    if normalized == "AUD":
        return settings.fx_usd_to_aud
    if normalized == "CAD":
        return settings.fx_usd_to_cad
    if normalized == "GBP":
        return settings.fx_usd_to_gbp
    return 1
