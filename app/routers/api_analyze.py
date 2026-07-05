import logging
import base64
import statistics
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from starlette.datastructures import UploadFile

from app.schemas.api_analysis import (
    ApiAlternativeMatchResponse,
    ApiAnalyzeDiagnosticsResponse,
    ApiAnalyzeRequest,
    ApiAnalyzeResponse,
    ApiMarketCompResponse,
    ApiMarketSummaryResponse,
    ApiReviewResponse,
)
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
from app.services.pricing.provider_factory import get_pricing_provider


router = APIRouter(prefix="/api", tags=["Analyze API"])
root_router = APIRouter(tags=["Analyzer API"])
logger = logging.getLogger("collectiq.api.analyze")

SUPPORTED_CATEGORIES = {
    "pokemon",
    "pokemon card",
    "tcg",
    "tcg card",
    "trading card",
    "sports card",
    "coin",
    "comic",
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


@root_router.post(
    "/analyze",
    response_model=ApiAnalyzeResponse,
    summary="Analyze a collectible image from the production Analyzer API contract",
)
async def analyze_collectible_root(request: Request) -> ApiAnalyzeResponse:
    payload = await _payload_from_request(request)
    return await _analyze_collectible(payload)


async def _analyze_collectible(payload: ApiAnalyzeRequest) -> ApiAnalyzeResponse:
    _validate_contract(payload)
    started_at = time.perf_counter()

    try:
        pipeline_result = BackendAnalyzerService().analyze(payload)
        provider = pipeline_result.provider
        recognition = pipeline_result.recognition
        pricing = get_pricing_provider().price(recognition)
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

    return ApiAnalyzeResponse(
        id=f"backend-{uuid4()}",
        itemName=recognition.title,
        title=recognition.title,
        category=recognition.category,
        manufacturer=recognition.brand,
        year=recognition.year,
        series=recognition.series,
        variant=recognition.edition,
        estimatedValue=pricing.estimatedMarketValue,
        estimated_value=pricing.estimatedMarketValue,
        currency=pricing.currency,
        tags=recognition.detectedObjects,
        description=recognition.description,
        attributes=_provider_neutral_attributes(recognition),
        images=[],
        rawProviderPayload={
            "provider": diagnostics.aiProvider,
            "pipelineStages": pipeline_result.stages,
        },
        lowEstimate=pricing.lowEstimate,
        highEstimate=pricing.highEstimate,
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
        },
    )


def _validate_contract(payload: ApiAnalyzeRequest) -> None:
    request = payload.request
    image = payload.image

    if not request.imagePath.strip() or not image.localFilePath.strip():
        raise _api_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_image",
            "Image metadata is required.",
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
        return [
            ApiMarketCompResponse(
                source=pricing.pricingSource,
                title="Market pricing reference",
                soldPrice=max(1, pricing.estimatedMarketValue),
                currency=pricing.currency,
                soldDate=pricing.lastUpdated,
                condition="Unknown",
                url=None,
            )
        ]

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
    return ApiAnalyzeDiagnosticsResponse(
        aiProvider=getattr(
            provider,
            "provider_name",
            getattr(recognition, "aiProvider", "unknown"),
        ),
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
