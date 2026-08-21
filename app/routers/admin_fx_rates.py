from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.routers.admin_auth import require_admin_job_token
from app.services.pricing.fx_rate_service import FxRateService, FxRateServiceError


router = APIRouter(prefix="/admin/pricing/fx-rates", tags=["Admin"])


@router.post("/refresh")
def refresh_fx_rates(
    _admin: dict[str, Any] = Depends(require_admin_job_token),
) -> dict[str, Any]:
    """Meant to run once a day via a Render cron -- fetches today's rates
    from Frankfurter and upserts them. currency_conversion.py's static
    env-var rates remain the fallback for any day this hasn't run yet."""
    service = FxRateService()
    try:
        rows_written = service.refresh_latest()
    except FxRateServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "fx_rate_refresh_failed",
                "message": str(exc),
                "retryable": True,
            },
        ) from exc
    return {"success": True, "rowsWritten": rows_written}


@router.post("/backfill")
def backfill_fx_rates(
    start_date: str = Query(..., alias="startDate"),
    end_date: str = Query(..., alias="endDate"),
    _admin: dict[str, Any] = Depends(require_admin_job_token),
) -> dict[str, Any]:
    """One-time (or re-runnable) historical backfill so existing portfolio
    value history can be converted using the rate that was actually in
    effect on each date, not just today's rate applied backward. Dates are
    YYYY-MM-DD. Safe to re-run -- upserts on (rate_date, currency)."""
    service = FxRateService()
    try:
        rows_written = service.backfill_historical(
            start_date=start_date, end_date=end_date
        )
    except FxRateServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "fx_rate_backfill_failed",
                "message": str(exc),
                "retryable": True,
            },
        ) from exc
    return {"success": True, "rowsWritten": rows_written}
