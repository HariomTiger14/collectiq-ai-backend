from __future__ import annotations

from app.schemas.pricing import (
    RepriceComparableSaleResponse,
    RepricePricingResponse,
    RepriceRequest,
    RepriceResponse,
)
from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.base_pricing_provider import (
    EmptyMarketDataError,
    PricingProviderError,
    PricingProviderRateLimitError,
    PricingProviderTimeoutError,
    PricingProviderUnavailableError,
    PricingResult,
)
from app.services.pricing.currency_conversion import (
    convert_pricing_result,
    normalize_display_currency,
)
from app.services.pricing.provider_factory import get_pricing_provider


class RepriceValidationError(Exception):
    """Raised when corrected identity fields are not sufficient to price."""


class RepriceService:
    def reprice(self, request: RepriceRequest) -> RepriceResponse:
        recognition = _recognition_from_request(request)
        display_currency = normalize_display_currency(
            request.displayCurrency or request.previousCurrency
        )
        try:
            pricing = get_pricing_provider().price(recognition)
        except PricingProviderUnavailableError as error:
            pricing = _unavailable_pricing(
                recognition,
                status="provider_not_configured",
                source="not_configured",
                reason=str(error),
                reason_code="PROVIDER_NOT_CONFIGURED",
                currency=display_currency,
            )
        except EmptyMarketDataError as error:
            pricing = _unavailable_pricing(
                recognition,
                status="no_market_match",
                source="market",
                reason=str(error),
                reason_code="NO_MARKET_MATCH",
                currency=display_currency,
            )
        except (PricingProviderTimeoutError, PricingProviderRateLimitError) as error:
            raise error
        except (PricingProviderError, ValueError) as error:
            pricing = _unavailable_pricing(
                recognition,
                status="lookup_failed",
                source="market",
                reason=str(error),
                reason_code="LOOKUP_FAILED",
                currency=display_currency,
            )
        pricing = convert_pricing_result(pricing, target_currency=display_currency)

        return RepriceResponse(
            itemId=request.itemId,
            correctionSource=request.correctionSource,
            identity=request.identity,
            pricing=_response_from_pricing(pricing),
        )


def _recognition_from_request(request: RepriceRequest) -> RecognitionResult:
    identity = request.identity
    title = identity.title.strip()
    category = identity.category.strip()
    if len(title) < 2:
        raise RepriceValidationError("Title is required before repricing.")
    if len(category) < 2:
        raise RepriceValidationError("Category is required before repricing.")
    condition = (identity.condition or "Unknown").strip() or "Unknown"
    detected = [category, title]
    if identity.brand:
        detected.append(identity.brand.strip())
    if identity.setName:
        detected.append(identity.setName.strip())

    return RecognitionResult(
        title=title,
        category=category,
        confidence=95,
        estimatedValue=0,
        condition=condition,
        recommendation="Review corrected details before saving valuation.",
        description=f"Manual correction for {title}.",
        detectedObjects=[value for value in detected if value],
        aiProvider="manual_correction",
        processingTimeMs=0,
        primaryMatch=title,
        alternativeMatches=[],
        confidenceExplanation="User-corrected identity fields were used for repricing.",
        detectionQuality="Manual correction",
        aiReasoning="Manual correction bypasses AI identity uncertainty for pricing lookup.",
        year=_clean(identity.year),
        brand=_clean(identity.brand),
        setName=_clean(identity.setName),
        series=_clean(identity.series),
        cardNumber=_clean(identity.cardNumber or identity.sku or identity.upc),
        playerOrCharacter=_clean(identity.playerOrCharacter),
        rarity=_clean(identity.rarity),
        estimatedGrade=_clean(identity.estimatedGrade),
        language=_clean(identity.language),
        edition=_clean(identity.edition),
        notes=_clean(identity.notes),
        fieldConfidence={
            "title": 100,
            "category": 100,
            "condition": 100 if condition != "Unknown" else 60,
        },
        confidenceLevel="High",
    )


def _response_from_pricing(pricing: PricingResult) -> RepricePricingResponse:
    available = pricing.valuationStatus == "market_estimated" and pricing.estimatedMarketValue > 0
    reason_code = None if available else _reason_code(pricing)
    display_message = None if available else _unavailable_message(pricing)
    value = pricing.estimatedMarketValue if available else None
    low = pricing.lowEstimate if available else None
    high = pricing.highEstimate if available else None
    diagnostics = dict(pricing.providerDiagnostics)
    valuation_strategy = diagnostics.get("valuationStrategy") or (
        "market_estimated" if available else "unavailable"
    )
    attribution = diagnostics.get("attributionText") or (
        f"Pricing data powered by {pricing.pricingSource}" if available else ""
    )
    return RepricePricingResponse(
        status="available" if available else "unavailable",
        reasonCode=reason_code,
        displayMessage=display_message,
        estimatedMarketValue=value,
        lowEstimate=low,
        highEstimate=high,
        currency=pricing.currency,
        displayString=_display_string(value, pricing.currency),
        confidenceScore=round(pricing.pricingConfidence / 100, 2),
        pricingConfidence=pricing.pricingConfidence,
        valuationStrategy=valuation_strategy,
        pricingSource={
            "name": pricing.pricingSource,
            "attributionText": attribution,
            "lastChecked": pricing.lastUpdated,
        },
        originalMarketPayload={
            "price": _pricing_original_value(pricing, value, diagnostics),
            "currency": getattr(pricing, "originalCurrency", None)
            or diagnostics.get("originalCurrency")
            or pricing.currency,
            "exchangeRateUsed": getattr(
                pricing,
                "exchangeRateUsed",
                _number_or(1, diagnostics.get("exchangeRateUsed")),
            ),
            "exchangeRateDate": getattr(pricing, "exchangeRateDate", None)
            or diagnostics.get("exchangeRateDate")
            or pricing.lastUpdated,
            "displayCurrency": pricing.currency,
        },
        matchMetadata={
            "reason": diagnostics.get("priceExplanation")
            or diagnostics.get("confidenceCalculation")
            or display_message,
            "lowEstimate": low,
            "highEstimate": high,
            "comparableCount": _int_or(len(pricing.comparableSales), diagnostics.get("comparableCount")),
            "minimumSoldCompsRule": diagnostics.get("minimumSoldCompsRule"),
        },
        comparableSales=[
            RepriceComparableSaleResponse(
                source=sale.source,
                title=sale.title,
                soldPrice=sale.soldPrice,
                currency=sale.currency,
                soldDate=sale.soldDate,
                condition=sale.condition,
                url=sale.url,
            )
            for sale in pricing.comparableSales
        ],
        diagnostics=diagnostics,
    )


def _unavailable_pricing(
    recognition: RecognitionResult,
    *,
    status: str,
    source: str,
    reason: str,
    reason_code: str,
    currency: str,
) -> PricingResult:
    from app.services.pricing.base_pricing_provider import utc_timestamp

    return PricingResult(
        estimatedMarketValue=0,
        lowEstimate=0,
        highEstimate=0,
        currency=currency,
        pricingSource=source,
        pricingConfidence=0,
        lastUpdated=utc_timestamp(),
        valuationStatus=status,
        valuationSource=source,
        aiEstimatedValue=None,
        marketTrend="Unknown",
        sourceCount=0,
        pricingAge="unknown",
        comparableSales=[],
        fallbackUsed=False,
        cacheStatus="unavailable",
        providerDiagnostics={
            "providerCount": "0",
            "providers": source,
            "fallbackUsed": "false",
            "fallbackReason": reason,
            "reasonCode": reason_code,
            "cacheStatus": "unavailable",
            "responseTimeMs": "0",
            "comparableCount": "0",
            "confidenceCalculation": reason,
            "priceExplanation": reason,
            "recognitionTitle": recognition.title,
            "recognitionCategory": recognition.category,
        },
    )


def _reason_code(pricing: PricingResult) -> str:
    explicit = pricing.providerDiagnostics.get("reasonCode")
    if explicit:
        return explicit
    return pricing.valuationStatus.upper()


def _unavailable_message(pricing: PricingResult) -> str:
    return (
        pricing.providerDiagnostics.get("fallbackReason")
        or pricing.providerDiagnostics.get("priceExplanation")
        or "Value unavailable: trusted pricing evidence was not found."
    )


def _display_string(value: float | None, currency: str) -> str | None:
    if value is None or value <= 0:
        return None
    normalized = currency.strip().upper() or "AUD"
    return f"{normalized} ${value:,.2f}"


def _number_or(fallback, value):
    if value in (None, ""):
        return fallback


def _pricing_original_value(
    pricing: PricingResult,
    fallback_value: float | None,
    diagnostics: dict,
):
    original_value = getattr(pricing, "originalMarketValue", None)
    if original_value is not None:
        return original_value
    return _number_or(fallback_value, diagnostics.get("originalPrice"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _int_or(fallback: int, value) -> int:
    if value in (None, ""):
        return fallback
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _clean(value: str | None) -> str | None:
    parsed = (value or "").strip()
    return parsed or None
