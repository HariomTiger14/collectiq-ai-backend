import logging
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status

from app.core.config import (
    ALLOWED_CONTENT_TYPES,
    ALLOWED_EXTENSIONS,
    CHUNK_SIZE,
    MAX_IMAGE_BYTES,
    UPLOAD_DIR,
)
from app.schemas.scanner import ScannerAnalysisResponse
from app.services.ai.openai_recognition_provider import (
    AIProviderNotConfiguredError,
    OpenAIInvalidResponseError,
    OpenAIProviderError,
    OpenAITimeoutError,
)
from app.services.ai.provider_factory import get_ai_recognition_provider
from app.services.pricing.provider_factory import get_pricing_provider


router = APIRouter(prefix="/scanner", tags=["Scanner"])
logger = logging.getLogger("collectiq.scanner")


def ensure_upload_dir() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@router.post(
    "/analyze",
    response_model=ScannerAnalysisResponse,
    summary="Analyze an uploaded scanner image",
)
async def analyze_scanner_image(
    request: Request,
    image: Annotated[
        UploadFile,
        File(
            description="Scanner image upload. Supported formats: jpg, jpeg, png. Maximum size: 10MB.",
        ),
    ],
) -> ScannerAnalysisResponse:
    extension = Path(image.filename or "").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS or image.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Invalid file type. Upload a jpg, jpeg, or png image.",
        )

    ensure_upload_dir()
    filename = f"{uuid4()}{extension}"
    destination = UPLOAD_DIR / filename

    total_bytes = 0
    try:
        with destination.open("wb") as uploaded_file:
            while chunk := await image.read(CHUNK_SIZE):
                total_bytes += len(chunk)
                if total_bytes > MAX_IMAGE_BYTES:
                    uploaded_file.close()
                    destination.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Image is too large. Maximum upload size is 10MB.",
                    )
                uploaded_file.write(chunk)
    finally:
        await image.close()

    image_url = str(request.url_for("uploads", path=filename))
    try:
        recognition = get_ai_recognition_provider().recognize(destination)
        pricing = get_pricing_provider().price(recognition)
    except ValueError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except AIProviderNotConfiguredError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc),
        ) from exc
    except OpenAITimeoutError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except OpenAIInvalidResponseError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except OpenAIProviderError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    logger.info(
        "Scanner analysis completed filename=%s processingTimeMs=%s aiProvider=%s",
        filename,
        recognition.processingTimeMs,
        recognition.aiProvider,
    )

    return ScannerAnalysisResponse(
        success=True,
        filename=filename,
        imageUrl=image_url,
        title=recognition.title,
        category=recognition.category,
        confidence=recognition.confidence,
        estimatedValue=pricing.estimatedMarketValue,
        condition=recognition.condition,
        recommendation=recognition.recommendation,
        description=recognition.description,
        detectedObjects=recognition.detectedObjects,
        aiProvider=recognition.aiProvider,
        processingTimeMs=recognition.processingTimeMs,
        primaryMatch=recognition.primaryMatch,
        alternativeMatches=[
            {
                "title": match.title,
                "category": match.category,
                "confidence": match.confidence,
                "reason": match.reason,
            }
            for match in recognition.alternativeMatches
        ],
        confidenceExplanation=recognition.confidenceExplanation,
        detectionQuality=recognition.detectionQuality,
        aiReasoning=recognition.aiReasoning,
        year=recognition.year,
        brand=recognition.brand,
        setName=recognition.setName,
        series=recognition.series,
        manufacturer=recognition.brand,
        estimated_value_low=pricing.lowEstimate,
        estimated_value_high=pricing.highEstimate,
        cardNumber=recognition.cardNumber,
        playerOrCharacter=recognition.playerOrCharacter,
        rarity=recognition.rarity,
        estimatedGrade=recognition.estimatedGrade,
        language=recognition.language,
        edition=recognition.edition,
        country=recognition.country,
        mint=recognition.mint,
        material=recognition.material,
        notes=recognition.notes,
        pricing={
            "estimatedMarketValue": pricing.estimatedMarketValue,
            "lowEstimate": pricing.lowEstimate,
            "highEstimate": pricing.highEstimate,
            "currency": pricing.currency,
            "pricingSource": pricing.pricingSource,
            "pricingConfidence": pricing.pricingConfidence,
            "lastUpdated": pricing.lastUpdated,
        },
    )
