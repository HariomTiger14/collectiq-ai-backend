from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.routers.admin_auth import require_admin_job_token
from app.services.alerts.price_alert_evaluation_service import (
    PriceAlertEvaluationService,
)
from app.services.push.price_alert_push_service import (
    PriceAlertPushService,
    PushNotificationError,
)


router = APIRouter(prefix="/admin/push", tags=["Admin Push"])


@router.post("/price-alerts/evaluate")
async def evaluate_price_alerts(
    dry_run: bool = Query(False, alias="dryRun"),
    limit: int = Query(1000, ge=1, le=5000),
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    """Flip saved alerts whose condition is now met to `triggered`."""
    summary = PriceAlertEvaluationService().evaluate_and_flag(
        limit=limit,
        dry_run=dry_run,
    )
    return summary.to_dict()


@router.post("/price-alerts/run")
async def run_price_alert_push_job(
    dry_run: bool = Query(False, alias="dryRun"),
    evaluate: bool = Query(True, alias="evaluate"),
    limit: int = Query(50, ge=1, le=500),
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    # Full pipeline for the scheduler: evaluate saved alerts (flip to
    # triggered), then dispatch pushes for triggered rows. Evaluation is
    # best-effort so a Supabase hiccup never blocks dispatch of rows that are
    # already triggered.
    evaluation: dict | None = None
    if evaluate:
        try:
            evaluation = PriceAlertEvaluationService().evaluate_and_flag(
                limit=1000,
                dry_run=dry_run,
            ).to_dict()
        except Exception as error:  # noqa: BLE001 - best-effort, keep dispatching
            evaluation = {"error": str(error)}
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

    result = {**summary.to_dict(), "dryRun": dry_run}
    if evaluation is not None:
        result["evaluation"] = evaluation
    return result


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


@router.get("/history")
async def list_push_delivery_history(
    limit: int = Query(25, ge=1, le=100),
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    try:
        return PriceAlertPushService().delivery_history(limit=limit)
    except PushNotificationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "push_history_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.post("/devices/{device_id}/disable")
async def disable_push_device_registration(
    device_id: str,
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    try:
        return PriceAlertPushService().disable_device_registration(device_id)
    except PushNotificationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "push_device_cleanup_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error
