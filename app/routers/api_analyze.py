import logging
import base64
import statistics
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from starlette.datastructures import UploadFile

from app.services.subscription.subscription_service import SubscriptionService

from app.schemas.api_analysis import (
    ApiAlternativeMatchResponse,
    ApiAnalyzeDiagnosticsResponse,
    ApiAnalyzeRequest,
    ApiAnalyzeResponse,
    ApiMarketCompResponse,
    ApiMarketSummaryResponse,
    ApiPricingQuoteRequest,
    ApiPricingQuoteResponse,
    ApiReviewResponse,
)
from app.core.config import settings
from app.services.ai.base_recognition_service import RecognitionResult
from app.services.ai.openai_recognition_provider import (
    AIProviderNotConfiguredError,
    OpenAIInvalidResponseError,
    OpenAIProviderError,
    OpenAITimeoutError,
)
from app.services.analyzer.backend_analyzer_service import BackendAnalyzerService
from app.services.analyzer.errors import AnalyzerPipelineError
from app.services.pricing.base_pricing_provider import (
    EmptyMarketDataError,
    PricingProviderError,
    PricingProviderRateLimitError,
    PricingProviderTimeoutError,
    PricingProviderUnavailableError,
)
from app.services.pricing.cache_policy import pricing_cache_policy
from app.services.pricing.currency_conversion import (
    convert_pricing_result,
    normalize_display_currency,
)
from app.services.pricing.provider_factory import get_pricing_provider
from app.services.pricing.shared_cache_repository import (
    SharedPricingCacheError,
    SharedPricingCacheRepository,
    with_shared_cache_status,
)


router = APIRouter(prefix="/api", tags=["Analyze API"])
root_router = APIRouter(tags=["Analyzer API"])
logger = logging.getLogger("collectiq.api.analyze")

_subscription_service = SubscriptionService()


def _enforce_scan_quota(request: Request) -> None:
    """Server-side monthly scan cap for FREE users (anti-abuse).

    Fail-open: only blocks when a signed-in free user is positively over the
    monthly limit. A missing/invalid token or any error lets the scan proceed,
    so scanning never breaks because of this check. Pro/premium are unlimited.
    """
    authorization = request.headers.get("authorization")
    if not authorization or not authorization.lower().startswith("bearer "):
        return
    token = authorization.split(" ", 1)[1].strip()
    try:
        result = _subscription_service.check_scan_allowed(
            token,
            free_monthly_limit=settings.subscription_free_monthly_scan_limit,
        )
    except Exception as error:  # noqa: BLE001 — fail open on any error
        logger.warning("scan quota check skipped (fail-open): %s", error)
        return
    if not result.get("allowed", True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Monthly scan limit reached for the free plan.",
        )
_shared_pricing_cache = SharedPricingCacheRepository()

SUPPORTED_CATEGORIES = {
    "pokemon",
    "pokemon card",
    "tcg",
    "tcg card",
    "trading card",
    "sports card",
    "coin",
    "comic",
    "game",
    "video game",
    "video games",
    "lego",
    "funko",
    "memorabilia",
    "toy",
    "figure",
    "other",
}


@router.post(
    "/analyze",
    response_model=ApiAnalyzeResponse,
    summary="Analyze a collectible image from the Flutter backend contract",
)
async def analyze_collectible(payload: ApiAnalyzeRequest) -> ApiAnalyzeResponse:
    return await _analyze_collectible(payload)


@router.post(
    "/pricing/quote",
    response_model=ApiPricingQuoteResponse,
    summary="Refresh market pricing for user-confirmed collectible details",
)
async def quote_collectible_pricing(
    payload: ApiPricingQuoteRequest,
) -> ApiPricingQuoteResponse:
    return _quote_collectible_pricing(payload)


@root_router.post(
    "/analyze",
    response_model=ApiAnalyzeResponse,
    summary="Analyze a collectible image from the production Analyzer API contract",
)
async def analyze_collectible_root(request: Request) -> ApiAnalyzeResponse:
    _enforce_scan_quota(request)
    payload = await _payload_from_request(request)
    return await _analyze_collectible(
        payload,
        request_payload_type=request.headers.get("content-type", "application/json"),
    )


async def _analyze_collectible(
    payload: ApiAnalyzeRequest,
    *,
    request_payload_type: str = "application/json",
) -> ApiAnalyzeResponse:
    _validate_contract(payload)
    started_at = time.perf_counter()
    trace_id = _trace_id_for(payload)
    logger.info(
        "analyze request traceId=%s environment=%s aiProvider=%s "
        "allowMockAnalyzer=%s payloadType=%s images=%s",
        trace_id,
        settings.environment,
        settings.ai_provider,
        settings.allow_mock_analyzer,
        _safe_payload_type(request_payload_type),
        _image_trace_details(payload),
    )

    try:
        pipeline_result = BackendAnalyzerService().analyze(payload)
        provider = pipeline_result.provider
        recognition = pipeline_result.recognition
        display_currency = _target_display_currency(payload)
        pricing = _price_recognition(
            recognition,
            trace_id=trace_id,
            display_currency=display_currency,
        )
        logger.info(
            "analyze response traceId=%s selectedAnalyzerProvider=%s "
            "aiProvider=%s itemName=%s confidence=%s valuationStatus=%s "
            "valuationSource=%s finalValue=%s",
            trace_id,
            getattr(provider, "provider_name", "unknown"),
            getattr(recognition, "aiProvider", "unknown"),
            recognition.title,
            recognition.confidence,
            pricing.valuationStatus,
            pricing.valuationSource,
            pricing.estimatedMarketValue,
        )
    except AnalyzerPipelineError as exc:
        raise _api_error(
            exc.status_code,
            exc.code,
            exc.message,
            retryable=exc.retryable,
            details=exc.details,
        ) from exc
    except AIProviderNotConfiguredError as exc:
        raise _api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "provider_unavailable",
            str(exc),
            retryable=False,
        ) from exc
    except OpenAITimeoutError as exc:
        raise _api_error(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "timeout",
            str(exc),
            retryable=True,
        ) from exc
    except OpenAIInvalidResponseError as exc:
        raise _api_error(
            status.HTTP_502_BAD_GATEWAY,
            "provider_unavailable",
            str(exc),
            retryable=True,
        ) from exc
    except OpenAIProviderError as exc:
        raise _api_error(
            status.HTTP_502_BAD_GATEWAY,
            "provider_unavailable",
            str(exc),
            retryable=True,
        ) from exc
    except PricingProviderTimeoutError as exc:
        raise _api_error(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "timeout",
            str(exc),
            retryable=True,
        ) from exc
    except PricingProviderRateLimitError as exc:
        raise _api_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "quota_exceeded",
            str(exc),
            retryable=True,
        ) from exc
    except PricingProviderUnavailableError as exc:
        raise _api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "provider_unavailable",
            str(exc),
            retryable=True,
        ) from exc
    except EmptyMarketDataError as exc:
        raise _api_error(
            status.HTTP_502_BAD_GATEWAY,
            "provider_unavailable",
            str(exc),
            retryable=True,
        ) from exc
    except PricingProviderError as exc:
        raise _api_error(
            status.HTTP_502_BAD_GATEWAY,
            "provider_unavailable",
            str(exc),
            retryable=True,
        ) from exc
    except ValueError as exc:
        raise _api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "provider_unavailable",
            str(exc),
            retryable=True,
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise _api_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "unknown",
            "Unable to analyze collectible right now.",
            retryable=True,
        ) from exc

    comparable_sales = _comparable_sales_from_pricing(pricing)

    market_summary = ApiMarketSummaryResponse(
        averagePrice=_average_price(comparable_sales, pricing.estimatedMarketValue),
        medianPrice=_median_price(comparable_sales, pricing.estimatedMarketValue),
        lowPrice=pricing.lowEstimate,
        highPrice=pricing.highEstimate,
        salesCount=len(comparable_sales),
        trendLabel=pricing.marketTrend,
        confidence=pricing.pricingConfidence,
        lastUpdated=pricing.lastUpdated,
        sources=_market_sources(pricing),
        comps=comparable_sales,
    )

    total_processing_time_ms = int((time.perf_counter() - started_at) * 1000)
    diagnostics = _diagnostics_response(
        provider=provider,
        recognition=recognition,
        pricing=pricing,
        confidence_level=_confidence_level(recognition.confidence),
        total_processing_time_ms=total_processing_time_ms,
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Analyze completed provider=%s model=%s latencyMs=%s "
            "overallProcessingTimeMs=%s pricingProvider=%s pricingResponseTimeMs=%s "
            "pricingProviderCount=%s pricingFallbackUsed=%s pricingCacheStatus=%s "
            "pricingFreshness=%s pricingFallbackReason=%s",
            diagnostics.aiProvider,
            diagnostics.aiModel,
            diagnostics.aiLatencyMs,
            diagnostics.totalLatencyMs,
            diagnostics.pricingProvider,
            diagnostics.pricingProviderLatencyMs,
            diagnostics.pricingProviderCount,
            diagnostics.pricingFallbackUsed,
            diagnostics.pricingCacheStatus,
            diagnostics.pricingFreshness,
            diagnostics.pricingFallbackReason or "",
        )

    ai_estimated_value = recognition.estimatedValue if recognition.estimatedValue > 0 else None
    market_estimated_value = (
        pricing.estimatedMarketValue if pricing.valuationStatus == "market_estimated" else None
    )
    display_value = market_estimated_value or ai_estimated_value or 0
    low_estimate = pricing.lowEstimate if market_estimated_value else 0
    high_estimate = pricing.highEstimate if market_estimated_value else 0
    valuation_strategy = (
        "sold_completed" if market_estimated_value else "unavailable"
    )
    reason_code = (
        None
        if market_estimated_value
        else (
            diagnostics.pricingFallbackReason
            or pricing.valuationStatus.upper()
        )
    )
    display_string = _format_currency_display(display_value, pricing.currency)
    original_market_value = pricing.originalMarketValue or display_value
    original_currency = pricing.originalCurrency or pricing.currency
    source_display_string = _format_currency_display(
        original_market_value,
        original_currency,
    )
    attribution_text = (
        f"Pricing data powered by {diagnostics.pricingProvider}"
        if market_estimated_value and diagnostics.pricingProvider
        else None
    )
    cache_policy = pricing_cache_policy(
        category=recognition.category,
        valuation_status=pricing.valuationStatus,
    )

    return ApiAnalyzeResponse(
        id=f"backend-{uuid4()}",
        itemName=recognition.title,
        title=recognition.title,
        category=recognition.category,
        manufacturer=recognition.brand,
        year=recognition.year,
        series=recognition.series,
        variant=recognition.edition,
        estimatedValue=display_value,
        estimated_value=display_value,
        currency=pricing.currency,
        tags=recognition.detectedObjects,
        description=recognition.description,
        attributes=_provider_neutral_attributes(recognition),
        images=[],
        rawProviderPayload={
            "provider": diagnostics.aiProvider,
            "pricingProvider": diagnostics.pricingProvider,
            "pricingExplanation": diagnostics.pricingExplanation,
            "pricingFallbackReason": diagnostics.pricingFallbackReason,
            "reasonCode": reason_code,
            "valuationStrategy": valuation_strategy,
            "displayString": display_string,
            "cachePolicy": {
                "ttlSeconds": cache_policy.ttl_seconds,
                "expiresAt": None,
                "reason": cache_policy.reason,
            },
            "pricingSource": {
                "name": diagnostics.pricingProvider,
                "attributionText": attribution_text,
                "lastChecked": pricing.lastUpdated,
            },
            "originalMarket": {
                "price": original_market_value,
                "currency": original_currency,
                "exchangeRateUsed": pricing.exchangeRateUsed,
                "exchangeRateDate": pricing.exchangeRateDate or pricing.lastUpdated,
                "displayPrice": display_value,
                "displayCurrency": pricing.currency,
                "displayString": display_string,
                "sourceDisplayString": source_display_string,
            },
            "matchMetadata": {
                "reason": diagnostics.pricingExplanation,
                "lowEstimate": low_estimate,
                "highEstimate": high_estimate,
                "lowEstimateAud": low_estimate,
                "highEstimateAud": high_estimate,
                "lowEstimateOriginal": pricing.originalLowEstimate or low_estimate,
                "highEstimateOriginal": pricing.originalHighEstimate or high_estimate,
            },
            "pipelineStages": pipeline_result.stages,
            "photosUsed": len(pipeline_result.image_payloads),
            "photoRoles": [
                image.get("imageRole", "other")
                for image in pipeline_result.image_payloads
            ],
            **_selection_diagnostics(provider),
        },
        faceValue=recognition.faceValue,
        estimatedMarketValue=market_estimated_value,
        aiEstimatedValue=ai_estimated_value,
        valuationStatus=pricing.valuationStatus,
        valuationSource=pricing.valuationSource,
        askingPriceWarning=recognition.askingPriceWarning,
        valuationConfidence=(
            pricing.pricingConfidence
            if market_estimated_value
            else recognition.valuationConfidence
        ),
        lowEstimate=low_estimate,
        highEstimate=high_estimate,
        confidence=recognition.confidence,
        condition=recognition.condition,
        marketTrend=pricing.marketTrend,
        keyAttributes=_key_attributes(recognition),
        aiReview=ApiReviewResponse(
            primaryMatch=recognition.primaryMatch,
            confidenceExplanation=recognition.confidenceExplanation,
            detectionQuality=recognition.detectionQuality,
            reasoning=recognition.aiReasoning,
        ),
        alternatives=[
            ApiAlternativeMatchResponse(
                title=match.title,
                category=match.category,
                confidence=match.confidence,
                reason=match.reason,
            )
            for match in recognition.alternativeMatches
        ],
        recommendation=recognition.recommendation,
        marketSummary=market_summary,
        comparableSales=comparable_sales,
        imageUrl=None,
        timestamp=payload.request.timestamp,
        fieldConfidence=_field_confidence(recognition),
        confidenceLevel=diagnostics.confidenceLevel,
        lowConfidenceReasons=(
            recognition.lowConfidenceReasons
            or _low_confidence_reasons(recognition.confidence, recognition.detectionQuality)
        ),
        imageQualityIssues=(
            recognition.imageQualityIssues
            or _image_quality_issues(recognition.detectionQuality)
        ),
        scanRecommendations=(
            recognition.scanRecommendations
            or _scan_recommendations(recognition.confidence, recognition.detectionQuality)
        ),
        diagnostics=diagnostics,
    )


def _quote_collectible_pricing(
    payload: ApiPricingQuoteRequest,
) -> ApiPricingQuoteResponse:
    started_at = time.perf_counter()
    trace_id = f"pricing-{uuid4()}"
    recognition = _recognition_from_pricing_quote(payload)
    display_currency = normalize_display_currency(payload.displayCurrency)
    logger.info(
        "pricing quote request traceId=%s itemName=%s category=%s currency=%s",
        trace_id,
        recognition.title,
        recognition.category,
        display_currency,
    )
    pricing = _price_recognition(
        recognition,
        trace_id=trace_id,
        display_currency=display_currency,
    )
    logger.info(
        "pricing quote response traceId=%s status=%s source=%s value=%s latencyMs=%s",
        trace_id,
        pricing.valuationStatus,
        pricing.valuationSource,
        pricing.estimatedMarketValue,
        int((time.perf_counter() - started_at) * 1000),
    )
    return _pricing_quote_response(recognition, pricing)


def _recognition_from_pricing_quote(payload: ApiPricingQuoteRequest) -> RecognitionResult:
    title = payload.itemName.strip()
    if not title:
        raise _api_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_request",
            "itemName is required for pricing refresh.",
            retryable=False,
        )
    category = payload.category.strip() if payload.category else "Collectible"
    condition = (payload.condition or "Unknown").strip() or "Unknown"
    estimated_value = payload.estimatedValue or 0
    return RecognitionResult(
        title=title,
        category=category,
        confidence=90,
        estimatedValue=estimated_value,
        condition=condition,
        recommendation="Review the refreshed pricing before saving.",
        description="User-confirmed collectible details supplied for pricing refresh.",
        detectedObjects=[],
        aiProvider="user_review",
        processingTimeMs=0,
        primaryMatch=title,
        alternativeMatches=[],
        confidenceExplanation="User-confirmed details were used for pricing refresh.",
        detectionQuality="Reviewed",
        aiReasoning="Pricing-only refresh; no second image analysis was performed.",
        year=_blank_as_none(payload.year),
        brand=_blank_as_none(payload.brand or payload.manufacturer),
        setName=_blank_as_none(payload.setName),
        series=_blank_as_none(payload.series),
        cardNumber=_blank_as_none(payload.cardNumber),
        playerOrCharacter=_blank_as_none(payload.playerOrCharacter),
        rarity=_blank_as_none(payload.rarity),
        language=_blank_as_none(payload.language),
        edition=_blank_as_none(payload.edition),
        notes=_blank_as_none(payload.notes),
    )


def _pricing_quote_response(
    recognition: RecognitionResult,
    pricing,
) -> ApiPricingQuoteResponse:
    comparable_sales = _comparable_sales_from_pricing(pricing)
    market_estimated_value = (
        pricing.estimatedMarketValue if pricing.valuationStatus == "market_estimated" else None
    )
    ai_estimated_value = recognition.estimatedValue if recognition.estimatedValue > 0 else None
    display_value = market_estimated_value or ai_estimated_value or 0
    low_estimate = pricing.lowEstimate if market_estimated_value else 0
    high_estimate = pricing.highEstimate if market_estimated_value else 0
    valuation_strategy = "sold_completed" if market_estimated_value else "unavailable"
    pricing_explanation = (
        pricing.providerDiagnostics.get("priceExplanation")
        or pricing.providerDiagnostics.get("confidenceCalculation")
        or pricing.providerDiagnostics.get("fallbackReason")
    )
    reason_code = None if market_estimated_value else (
        pricing.providerDiagnostics.get("fallbackReason") or pricing.valuationStatus.upper()
    )
    display_string = _format_currency_display(display_value, pricing.currency)
    original_market_value = pricing.originalMarketValue or display_value
    original_currency = pricing.originalCurrency or pricing.currency
    source_display_string = _format_currency_display(
        original_market_value,
        original_currency,
    )
    attribution_text = (
        f"Pricing data powered by {pricing.pricingSource}"
        if market_estimated_value and pricing.pricingSource
        else None
    )
    cache_policy = pricing_cache_policy(
        category=recognition.category,
        valuation_status=pricing.valuationStatus,
    )
    pricing_json = {
        "estimatedMarketValue": market_estimated_value or 0,
        "lowEstimate": low_estimate,
        "highEstimate": high_estimate,
        "currency": pricing.currency,
        "pricingSource": pricing.pricingSource,
        "pricingConfidence": pricing.pricingConfidence,
        "lastUpdated": pricing.lastUpdated,
        "valuationStatus": pricing.valuationStatus,
        "valuationSource": pricing.valuationSource,
        "aiEstimatedValue": ai_estimated_value,
        "pricingExplanation": pricing_explanation,
        "reasonCode": reason_code,
        "valuationStrategy": valuation_strategy,
        "attributionText": attribution_text,
        "displayString": display_string,
        "originalPrice": original_market_value,
        "originalCurrency": original_currency,
        "exchangeRateUsed": pricing.exchangeRateUsed,
        "exchangeRateDate": pricing.exchangeRateDate or pricing.lastUpdated,
        "lowEstimateAud": low_estimate,
        "highEstimateAud": high_estimate,
        "cacheTtlSeconds": cache_policy.ttl_seconds,
        "cacheExpiresAt": None,
        "cachePolicyReason": cache_policy.reason,
    }
    market_summary = ApiMarketSummaryResponse(
        averagePrice=_average_price(comparable_sales, market_estimated_value or display_value),
        medianPrice=_median_price(comparable_sales, market_estimated_value or display_value),
        lowPrice=low_estimate,
        highPrice=high_estimate,
        salesCount=len(comparable_sales),
        trendLabel=pricing.marketTrend,
        confidence=pricing.pricingConfidence,
        lastUpdated=pricing.lastUpdated,
        sources=_market_sources(pricing),
        comps=comparable_sales,
    )

    return ApiPricingQuoteResponse(
        itemName=recognition.title,
        category=recognition.category,
        condition=recognition.condition,
        estimatedValue=display_value,
        currency=pricing.currency,
        estimatedMarketValue=market_estimated_value,
        aiEstimatedValue=ai_estimated_value,
        lowEstimate=low_estimate,
        highEstimate=high_estimate,
        valuationStatus=pricing.valuationStatus,
        valuationSource=pricing.valuationSource,
        valuationConfidence=pricing.pricingConfidence,
        marketTrend=pricing.marketTrend,
        pricing=pricing_json,
        marketSummary=market_summary,
        comparableSales=comparable_sales,
        rawProviderPayload={
            "pricingProvider": pricing.pricingSource,
            "pricingExplanation": pricing_explanation,
            "pricingFallbackReason": pricing.providerDiagnostics.get("fallbackReason"),
            "reasonCode": reason_code,
            "valuationStrategy": valuation_strategy,
            "displayString": display_string,
            "cachePolicy": {
                "ttlSeconds": cache_policy.ttl_seconds,
                "expiresAt": None,
                "reason": cache_policy.reason,
            },
            "pricingSource": {
                "name": pricing.pricingSource,
                "attributionText": attribution_text,
                "lastChecked": pricing.lastUpdated,
            },
            "originalMarket": {
                "price": original_market_value,
                "currency": original_currency,
                "exchangeRateUsed": pricing.exchangeRateUsed,
                "exchangeRateDate": pricing.exchangeRateDate or pricing.lastUpdated,
                "displayPrice": display_value,
                "displayCurrency": pricing.currency,
                "displayString": display_string,
                "sourceDisplayString": source_display_string,
            },
            "matchMetadata": {
                "reason": pricing_explanation,
                "lowEstimate": low_estimate,
                "highEstimate": high_estimate,
                "lowEstimateAud": low_estimate,
                "highEstimateAud": high_estimate,
                "lowEstimateOriginal": pricing.originalLowEstimate or low_estimate,
                "highEstimateOriginal": pricing.originalHighEstimate or high_estimate,
            },
            "diagnostics": {
                "pricingCacheStatus": pricing.cacheStatus,
                "pricingFreshness": pricing.pricingAge,
                "pricingProviderCount": pricing.sourceCount,
                "pricingFallbackUsed": pricing.fallbackUsed,
                "pricingComparableCount": len(pricing.comparableSales),
            },
        },
        timestamp=pricing.lastUpdated,
    )


async def _payload_from_request(request: Request) -> ApiAnalyzeRequest:
    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in content_type:
        return await _payload_from_multipart_request(request)

    body = await request.json()
    if not isinstance(body, dict):
        raise _api_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_request",
            "Analyze request body must be a JSON object.",
            retryable=False,
        )
    return ApiAnalyzeRequest(**body)


async def _payload_from_multipart_request(request: Request) -> ApiAnalyzeRequest:
    form = await request.form()
    upload = form.get("image")
    if not isinstance(upload, UploadFile):
        raise _api_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_image",
            "Multipart request must include an image file field named 'image'.",
            retryable=False,
        )

    image_bytes = await upload.read()
    await upload.close()
    file_name = upload.filename or "analyzer-upload.jpg"
    mime_type = upload.content_type or "application/octet-stream"
    image_source = str(form.get("imageSource") or "unknown")
    local_file_path = str(form.get("imagePath") or file_name)

    return ApiAnalyzeRequest(
        request={
            "imagePath": local_file_path,
            "imageSource": image_source,
            "requestedCategory": _optional_form_value(form.get("requestedCategory")),
            "appVersion": _optional_form_value(form.get("appVersion")),
            "deviceMetadata": {},
            "timestamp": str(form.get("timestamp") or ""),
        },
        image={
            "fileName": file_name,
            "mimeType": mime_type,
            "sizeBytes": len(image_bytes),
            "imageSource": image_source,
            "localFilePath": local_file_path,
            "base64Image": base64.b64encode(image_bytes).decode("ascii"),
            "imageRole": str(form.get("imageRole") or "front"),
        },
    )


def _validate_contract(payload: ApiAnalyzeRequest) -> None:
    request = payload.request
    images = [*payload.images]
    if payload.image is not None:
        images.insert(0, payload.image)

    if not request.imagePath.strip() or not images:
        raise _api_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_image",
            "Image metadata is required.",
            retryable=False,
        )
    if not any(
        image.localFilePath.strip()
        or (image.base64Image and image.base64Image.strip())
        or (image.base64Preview and image.base64Preview.strip())
        for image in images
    ):
        raise _api_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_image",
            "At least one image path or base64 image is required.",
            retryable=False,
        )

    requested_category = (request.requestedCategory or "").strip().lower()
    if requested_category and requested_category not in SUPPORTED_CATEGORIES:
        raise _api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_image",
            "Requested category is not supported for v1 analysis.",
            retryable=False,
            details={"requestedCategory": request.requestedCategory},
        )


def _api_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool,
    details: dict | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        },
    )


def _trace_id_for(payload: ApiAnalyzeRequest) -> str:
    metadata = payload.request.deviceMetadata or {}
    for key in ("scannerTraceId", "traceId", "requestId"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return str(uuid4())


def _safe_payload_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower() or "unknown"


def _image_trace_details(payload: ApiAnalyzeRequest) -> list[dict]:
    images = [*payload.images]
    if payload.image is not None:
        images.insert(0, payload.image)
    return [
        {
            "fileName": image.fileName,
            "mimeType": image.mimeType,
            "sizeBytes": image.sizeBytes,
            "imageRole": image.imageRole or "other",
            "hasBase64Image": bool(image.base64Image),
            "base64Length": len(image.base64Image or ""),
        }
        for image in images
    ]


def _optional_form_value(value) -> str | None:
    if value is None:
        return None
    parsed = str(value).strip()
    return parsed or None


def _key_attributes(recognition) -> dict[str, str]:
    attributes = {
        "year": recognition.year,
        "brand": recognition.brand,
        "setName": recognition.setName,
        "series": recognition.series,
        "cardNumber": recognition.cardNumber,
        "playerOrCharacter": recognition.playerOrCharacter,
        "rarity": recognition.rarity,
        "estimatedGrade": recognition.estimatedGrade,
        "language": recognition.language,
        "edition": recognition.edition,
        "country": recognition.country,
        "mint": recognition.mint,
        "material": recognition.material,
        "notes": recognition.notes,
    }
    return {
        key: value.strip()
        for key, value in attributes.items()
        if isinstance(value, str) and value.strip()
    }


def _provider_neutral_attributes(recognition) -> dict[str, str]:
    return {
        key: value
        for key, value in _key_attributes(recognition).items()
        if value.strip()
    }


def _selection_diagnostics(provider) -> dict[str, object]:
    diagnostics = getattr(provider, "selection_diagnostics", None)
    if not diagnostics:
        return {}
    if isinstance(diagnostics, dict) and "seedSource" not in diagnostics:
        return {"providerSelection": diagnostics}
    return {"mockSelection": diagnostics}


def _price_recognition(
    recognition,
    *,
    trace_id: str,
    display_currency: str,
):
    provider_name = settings.pricing_provider.strip().lower()
    lookup_query = _pricing_lookup_query(recognition)
    logger.info(
        "pricing lookup traceId=%s itemName=%s category=%s manufacturer=%s "
        "normalizedLookupQuery=%s attempted=%s selectedPricingProvider=%s",
        trace_id,
        recognition.title,
        recognition.category,
        recognition.brand or "",
        lookup_query,
        provider_name != "mock",
        provider_name,
    )
    if _recognition_too_uncertain_for_pricing(recognition):
        result = _valuation_placeholder(
            recognition,
            status="unavailable",
            source="not_priced",
            reason=(
                "Recognition was too uncertain for provider pricing. "
                "Capture a clearer image or confirm the item details before valuation."
            ),
        )
        logger.info(
            "pricing skipped traceId=%s status=%s itemName=%s confidence=%s",
            trace_id,
            result.valuationStatus,
            recognition.title,
            recognition.confidence,
        )
        return result
    if provider_name == "mock":
        result = _valuation_placeholder(
            recognition,
            status="provider_not_configured",
            source="not_configured",
            reason="PRICING_PROVIDER is mock; no real pricing source is connected.",
        )
        logger.info(
            "pricing result traceId=%s status=%s provider=%s finalMarketValue=%s",
            trace_id,
            result.valuationStatus,
            provider_name,
            result.estimatedMarketValue,
        )
        return result

    try:
        cached_result = _shared_pricing_cache.get(
            recognition,
            display_currency=display_currency,
        )
    except SharedPricingCacheError as exc:
        logger.warning(
            "pricing shared cache read failed traceId=%s error=%s",
            trace_id,
            exc,
        )
        cached_result = None
    if cached_result is not None:
        logger.info(
            "pricing shared cache hit traceId=%s itemName=%s cacheStatus=%s",
            trace_id,
            recognition.title,
            cached_result.cacheStatus,
        )
        return cached_result

    try:
        result = get_pricing_provider().price(recognition)
    except PricingProviderUnavailableError as exc:
        result = _valuation_placeholder(
            recognition,
            status="provider_not_configured",
            source="not_configured" if provider_name in {"auto", "real"} else provider_name,
            reason=str(exc),
        )
    except EmptyMarketDataError as exc:
        result = _valuation_placeholder(
            recognition,
            status="no_market_match",
            source=provider_name,
            reason=str(exc),
        )
    except (
        PricingProviderTimeoutError,
        PricingProviderRateLimitError,
        PricingProviderError,
        ValueError,
    ) as exc:
        result = _valuation_placeholder(
            recognition,
            status="lookup_failed",
            source=provider_name,
            reason=str(exc),
        )
    result = convert_pricing_result(result, target_currency=display_currency)
    try:
        _shared_pricing_cache.set(
            recognition,
            result,
            display_currency=display_currency,
        )
        result = with_shared_cache_status(result, "shared_miss")
    except SharedPricingCacheError as exc:
        logger.warning(
            "pricing shared cache write failed traceId=%s error=%s",
            trace_id,
            exc,
        )
    logger.info(
        "pricing result traceId=%s status=%s provider=%s source=%s "
        "marketValue=%s confidence=%s comparableCount=%s",
        trace_id,
        result.valuationStatus,
        provider_name,
        result.valuationSource,
        result.estimatedMarketValue,
        result.pricingConfidence,
        len(result.comparableSales),
    )
    return result


def _target_display_currency(payload: ApiAnalyzeRequest) -> str:
    metadata = payload.request.deviceMetadata or {}
    return normalize_display_currency(
        metadata.get("currency")
        or metadata.get("displayCurrency")
        or metadata.get("preferredCurrency")
        or metadata.get("regionCurrency")
    )


def _valuation_placeholder(
    recognition,
    *,
    status: str,
    source: str,
    reason: str,
):
    from app.services.pricing.base_pricing_provider import PricingResult, utc_timestamp

    return PricingResult(
        estimatedMarketValue=0,
        lowEstimate=0,
        highEstimate=0,
        currency="AUD",
        pricingSource=source,
        pricingConfidence=0,
        lastUpdated=utc_timestamp(),
        valuationStatus=status,
        valuationSource=source,
        aiEstimatedValue=recognition.estimatedValue
        if recognition.estimatedValue > 0
        else None,
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
            "cacheStatus": "unavailable",
            "responseTimeMs": "0",
            "comparableCount": "0",
            "confidenceCalculation": reason,
            "priceExplanation": reason,
        },
    )


def _pricing_lookup_query(recognition) -> str:
    parts = [
        recognition.title,
        recognition.brand,
        recognition.series,
        recognition.year,
        recognition.category,
        recognition.condition,
    ]
    return " ".join(
        part.strip()
        for part in parts
        if isinstance(part, str) and part.strip()
    )


def _blank_as_none(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _recognition_too_uncertain_for_pricing(recognition) -> bool:
    title = (recognition.title or "").strip().lower()
    category = (recognition.category or "").strip().lower()
    if not title or title in {"unknown", "unknown collectible", "unidentified collectible"}:
        return True
    if category in {"", "other", "unknown"} and recognition.confidence < 70:
        return True
    return recognition.confidence < 25


def _field_confidence(recognition) -> dict[str, int]:
    if recognition.fieldConfidence:
        return {
            key: _clamp_confidence(value)
            for key, value in recognition.fieldConfidence.items()
        }

    return {
        "itemName": _clamp_confidence(recognition.confidence),
        "category": _clamp_confidence(recognition.confidence),
        "brand": _clamp_confidence(recognition.confidence - 5),
        "setName": _clamp_confidence(recognition.confidence - 10),
        "year": _clamp_confidence(recognition.confidence - 12),
        "cardNumber": _clamp_confidence(recognition.confidence - 15),
        "condition": _clamp_confidence(recognition.confidence - 20),
        "estimatedGrade": _clamp_confidence(recognition.confidence - 25),
        "language": _clamp_confidence(recognition.confidence - 20),
        "edition": _clamp_confidence(recognition.confidence - 20),
    }


def _confidence_level(confidence: int) -> str:
    if confidence >= 90:
        return "High"
    if confidence >= 70:
        return "Medium"
    return "Low"


def _low_confidence_reasons(confidence: int, detection_quality: str) -> list[str]:
    reasons: list[str] = []
    if confidence < 90:
        reasons.append("Some collectible details could not be verified from the image.")
    normalized_quality = detection_quality.lower()
    if any(term in normalized_quality for term in ["fair", "blurry", "glare", "dark"]):
        reasons.append(detection_quality)
    if confidence < 70:
        reasons.append("Important identifiers such as set, number, or edition may be missing.")
    return reasons


def _image_quality_issues(detection_quality: str) -> list[str]:
    normalized = detection_quality.lower()
    issues: list[str] = []
    checks = {
        "blurry": "blurry image",
        "glare": "glare/reflections",
        "reflection": "glare/reflections",
        "cropped": "cropped edges",
        "dark": "dark image",
        "low resolution": "low resolution",
        "multiple": "multiple collectibles in one photo",
        "fair": "fine details may be hard to read",
    }
    for needle, label in checks.items():
        if needle in normalized and label not in issues:
            issues.append(label)
    return issues


def _scan_recommendations(confidence: int, detection_quality: str) -> list[str]:
    recommendations = [
        "Use bright, even lighting.",
        "Keep the collectible flat and fully inside the frame.",
    ]
    if confidence < 90 or _image_quality_issues(detection_quality):
        recommendations.append("Retake the image closer and avoid glare.")
        recommendations.append("Capture small text such as set name, date, card number, or mint mark.")
    return recommendations


def _clamp_confidence(value: int) -> int:
    return max(0, min(100, int(value)))


def _comparable_sales_from_pricing(pricing) -> list[ApiMarketCompResponse]:
    if not pricing.comparableSales:
        return []

    return [
        ApiMarketCompResponse(
            source=sale.source,
            title=sale.title,
            soldPrice=max(1, sale.soldPrice),
            currency=sale.currency,
            soldDate=sale.soldDate,
            condition=sale.condition,
            url=sale.url,
        )
        for sale in pricing.comparableSales
    ]


def _average_price(
    comparable_sales: list[ApiMarketCompResponse],
    fallback: int,
) -> int:
    if not comparable_sales:
        return fallback
    return max(1, round(statistics.mean(sale.soldPrice for sale in comparable_sales)))


def _median_price(
    comparable_sales: list[ApiMarketCompResponse],
    fallback: int,
) -> int:
    if not comparable_sales:
        return fallback
    return max(1, round(statistics.median(sale.soldPrice for sale in comparable_sales)))


def _market_sources(pricing) -> list[str]:
    sources = [
        source.strip()
        for source in pricing.pricingSource.split(",")
        if source.strip()
    ]
    return sources or [pricing.pricingSource]


def _diagnostics_response(
    *,
    provider,
    recognition,
    pricing,
    confidence_level: str,
    total_processing_time_ms: int,
) -> ApiAnalyzeDiagnosticsResponse:
    provider_selection = getattr(provider, "selection_diagnostics", {}) or {}
    selected_ai_provider = provider_selection.get("selectedProvider") or getattr(
        provider,
        "provider_name",
        getattr(recognition, "aiProvider", "unknown"),
    )
    return ApiAnalyzeDiagnosticsResponse(
        aiProvider=selected_ai_provider,
        aiModel=getattr(provider, "_model", "mock"),
        aiLatencyMs=_parse_int(
            getattr(recognition, "processingTimeMs", total_processing_time_ms),
            fallback=total_processing_time_ms,
        ),
        pricingProvider=pricing.providerDiagnostics.get(
            "providers",
            pricing.pricingSource,
        )
        or pricing.pricingSource,
        pricingProviderLatencyMs=_parse_optional_int(
            pricing.providerDiagnostics.get("providerResponseLatencyMs")
        ),
        pricingProviderCount=_parse_int(
            pricing.providerDiagnostics.get("providerCount"),
            fallback=pricing.sourceCount,
        ),
        pricingFallbackUsed=pricing.fallbackUsed,
        pricingFallbackReason=pricing.providerDiagnostics.get("fallbackReason") or None,
        pricingCacheStatus=pricing.cacheStatus,
        pricingFreshness=pricing.pricingAge,
        pricingProviderAgreement=_parse_optional_int(
            pricing.providerDiagnostics.get("providerAgreement")
        ),
        pricingVariancePercent=_parse_optional_int(
            pricing.providerDiagnostics.get("priceVariance")
        ),
        pricingMedianValue=_parse_optional_int(
            pricing.providerDiagnostics.get("medianPrice")
        ),
        pricingOutliersRemoved=_parse_optional_int(
            pricing.providerDiagnostics.get("outliersRemoved")
        ),
        pricingComparableCount=_parse_optional_int(
            pricing.providerDiagnostics.get("comparableCount")
        ),
        pricingConfidenceCalculation=pricing.providerDiagnostics.get(
            "confidenceCalculation"
        )
        or None,
        pricingExplanation=pricing.providerDiagnostics.get("priceExplanation") or None,
        pricingComparableQuality=pricing.providerDiagnostics.get("comparableQuality")
        or None,
        pricingMarketDataTypes=pricing.providerDiagnostics.get("marketDataTypes")
        or None,
        pricingTrustNotes=pricing.providerDiagnostics.get("trustNotes") or None,
        valuationStatus=pricing.valuationStatus,
        valuationSource=pricing.valuationSource,
        confidenceLevel=confidence_level,
        totalLatencyMs=total_processing_time_ms,
    )


def _parse_optional_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).split(",")[0].strip())
    except (TypeError, ValueError):
        return None


def _parse_int(value, *, fallback: int) -> int:
    parsed = _parse_optional_int(value)
    return fallback if parsed is None else parsed


def _format_currency_display(value: int | float | None, currency: str | None) -> str | None:
    if not value:
        return None
    normalized_currency = (currency or "AUD").strip().upper()
    amount = f"{float(value):,.2f}"
    if normalized_currency == "USD":
        return f"USD ${amount}"
    if normalized_currency == "GBP":
        return f"£{amount}"
    if normalized_currency == "CAD":
        return f"CAD ${amount}"
    if normalized_currency == "AUD":
        return f"${amount} AUD"
    return f"{normalized_currency} {amount}"
