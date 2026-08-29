from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.core.config import settings
from app.schemas.pricing import RepriceRequest, RepriceResponse
from app.services.pricing.base_pricing_provider import (
    PricingProviderRateLimitError,
    PricingProviderTimeoutError,
)
from app.services.pricing.currency_conversion import SUPPORTED_DISPLAY_CURRENCIES
from app.services.pricing.fx_rate_service import (
    TRACKED_CURRENCIES,
    FxRateService,
    FxRateServiceError,
)
from app.services.pricing.reprice_service import RepriceService, RepriceValidationError


router = APIRouter(prefix="/api/pricing", tags=["Pricing"])

_STATIC_FALLBACK_RATES = {
    "USD": 1.0,
    "AUD": settings.fx_usd_to_aud,
    "CAD": settings.fx_usd_to_cad,
    "GBP": settings.fx_usd_to_gbp,
}


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


@router.get("/fx-rates")
def get_fx_rates(
    from_date: str | None = Query(default=None, alias="fromDate"),
    to_date: str | None = Query(default=None, alias="toDate"),
) -> dict[str, Any]:
    """Every daily FX rate PackLox has stored in the requested range, plus
    each currency's most current rate -- the app fetches this once per
    currency change (or session) and does its own conversion arithmetic
    locally: today's rate for current totals, the rate that was actually in
    effect on a given date for historical chart points. See
    fx_rate_service.py's module docstring for why the client does the
    arithmetic rather than a new "converted portfolio total" endpoint.

    Defaults to the last 2 years if no range is given -- covers every chart
    range the app currently offers (1M/6M/MAX). Any currency/date this
    table has no row for yet (before the daily cron started, or a gap day)
    is simply absent from `rates`; the client falls back to `current` (or a
    hardcoded rate) for those, same posture as the backend's own static
    env-var fallback.
    """
    today = date.today()
    range_from = from_date or (today - timedelta(days=730)).isoformat()
    range_to = to_date or today.isoformat()

    service = FxRateService()
    current = dict(_STATIC_FALLBACK_RATES)
    rates: list[dict[str, Any]] = []
    if service.is_configured:
        try:
            current.update(service.current_rates())
        except FxRateServiceError:
            pass
        try:
            rates = service.rates_for_range(
                currencies=list(TRACKED_CURRENCIES),
                from_date=range_from,
                to_date=range_to,
            )
        except FxRateServiceError:
            rates = []

    return {
        "success": True,
        "supportedCurrencies": sorted(SUPPORTED_DISPLAY_CURRENCIES),
        "current": current,
        "rates": [
            {
                "date": row.get("rate_date"),
                "currency": row.get("currency"),
                "usdRate": row.get("usd_rate"),
            }
            for row in rates
            if isinstance(row, dict)
        ],
    }
