from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.routers.admin_auth import require_admin_job_token
from app.services.push.price_alert_push_service import (
    PriceAlertPushService,
    PushNotificationError,
)


router = APIRouter(prefix="/admin/push", tags=["Admin Push"])


@router.post("/price-alerts/run")
async def run_price_alert_push_job(
    dry_run: bool = Query(False, alias="dryRun"),
    limit: int = Query(50, ge=1, le=500),
    _admin: None = Depends(require_admin_job_token),
) -> dict:
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


@router.post("/test")
async def send_test_push_notification(
    dry_run: bool = Query(False, alias="dryRun"),
    limit: int = Query(10, ge=1, le=100),
    user_id: str | None = Query(None, alias="userId"),
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    try:
        summary = PriceAlertPushService().dispatch_test_notification(
            user_id=user_id,
            limit=limit,
            dry_run=dry_run,
        )
    except PushNotificationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "push_test_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error

    return {**summary.to_dict(), "dryRun": dry_run}


@router.post("/test-price-alert")
async def send_test_price_alert_push_notification(
    portfolio_item_id: str = Query(..., alias="portfolioItemId", min_length=1),
    dry_run: bool = Query(False, alias="dryRun"),
    limit: int = Query(10, ge=1, le=100),
    user_id: str | None = Query(None, alias="userId"),
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    try:
        summary = PriceAlertPushService().dispatch_test_price_alert_notification(
            portfolio_item_id=portfolio_item_id,
            user_id=user_id,
            limit=limit,
            dry_run=dry_run,
        )
    except PushNotificationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "push_test_price_alert_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error

    return {**summary.to_dict(), "dryRun": dry_run}
