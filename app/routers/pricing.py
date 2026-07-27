from fastapi import APIRouter, HTTPException, status

from app.schemas.pricing import RepriceRequest, RepriceResponse
from app.services.pricing.base_pricing_provider import (
    PricingProviderRateLimitError,
    PricingProviderTimeoutError,
)
from app.services.pricing.reprice_service import RepriceService, RepriceValidationError


router = APIRouter(prefix="/api/pricing", tags=["Pricing"])


@router.post("/reprice", response_model=RepriceResponse)
async def reprice_collectible(payload: RepriceRequest) -> RepriceResponse:
    try:
        return RepriceService().reprice(payload)
    except RepriceValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_reprice_identity",
                "message": str(error),
                "retryable": False,
            },
        ) from error
    except PricingProviderRateLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "pricing_rate_limited",
                "message": str(error),
                "retryable": True,
            },
        ) from error
    except PricingProviderTimeoutError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "pricing_timeout",
                "message": str(error),
                "retryable": True,
            },
        ) from error
