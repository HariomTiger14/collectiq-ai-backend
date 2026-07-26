from fastapi import APIRouter, Header, HTTPException, Query, status

from app.core.config import settings
from app.services.push.price_alert_push_service import (
    PriceAlertPushService,
    PushNotificationError,
)


router = APIRouter(prefix="/admin/push", tags=["Admin Push"])


@router.post("/price-alerts/run")
async def run_price_alert_push_job(
    dry_run: bool = Query(False, alias="dryRun"),
    limit: int = Query(50, ge=1, le=500),
    x_admin_token: str = Header("", alias="X-Admin-Token"),
) -> dict:
    if not settings.admin_job_token or x_admin_token != settings.admin_job_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "admin_token_required",
                "message": "A valid admin token is required.",
                "retryable": False,
            },
        )

    try:
        summary = PriceAlertPushService().dispatch_triggered_alerts(
            limit=limit,
            dry_run=dry_run,
        )
    except PushNotificationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "push_job_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error

    return {**summary.to_dict(), "dryRun": dry_run}
