from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from app.services.ops.observability import recorded_admin_job

from app.routers.admin_auth import (
    require_admin_job_permission,
    require_admin_permission,
)
from app.services.alerts.price_alert_evaluation_service import (
    PriceAlertEvaluationService,
)
from app.services.push.price_alert_push_service import (
    PriceAlertPushService,
    PushNotificationError,
)


router = APIRouter(prefix="/admin/push", tags=["Admin Push"])

TITLE_MAX = 120
BODY_MAX = 500
SEGMENT_PATTERN = "^(all|pro|inactive)$"


class BroadcastRequest(BaseModel):
    """JSON body for a broadcast.

    The message used to travel only as query parameters, which put the full
    title and body of every push an admin sent into request logs, proxy and
    CDN access logs, and browser history. A URL is not a private channel.

    Query parameters still work: the two callers that send them (the console
    before this deploys, and anything scripted against the documented route)
    must not break on the deploy that adds this. Body wins where both are
    given.
    """

    segment: str | None = Field(default=None, pattern=SEGMENT_PATTERN)
    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    body: str | None = Field(default=None, min_length=1, max_length=BODY_MAX)


class DirectSendRequest(BaseModel):
    """JSON body for a direct send to one user. See BroadcastRequest."""

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    body: str | None = Field(default=None, min_length=1, max_length=BODY_MAX)
    deviceId: str | None = Field(default=None, max_length=512)


def _resolve_required(body_value: str | None, query_value: str | None, field: str) -> str:
    """Body first, then the query parameter. 422 rather than an empty push."""
    value = (body_value if body_value is not None else query_value)
    if value is None or not str(value).strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "push_field_required",
                "message": f"`{field}` is required, in the JSON body or as a query parameter.",
                "retryable": False,
            },
        )
    return str(value)


@router.post("/price-alerts/evaluate")
async def evaluate_price_alerts(
    dry_run: bool = Query(True, alias="dryRun"),
    limit: int = Query(1000, ge=1, le=5000),
    _admin: dict[str, Any] = Depends(require_admin_job_permission("push:write")),
) -> dict:
    """Flip saved alerts whose condition is now met to `triggered`."""
    summary = PriceAlertEvaluationService().evaluate_and_flag(
        limit=limit,
        dry_run=dry_run,
    )
    return summary.to_dict()


@router.post("/price-alerts/run")
@recorded_admin_job("price-alerts-run")
async def run_price_alert_push_job(
    # Defaults to a dry run: a mistyped or truncated call must not put real
    # pushes on real devices. Every scheduler that means it passes
    # dryRun=false explicitly (see render.yaml).
    dry_run: bool = Query(True, alias="dryRun"),
    evaluate: bool = Query(True, alias="evaluate"),
    limit: int = Query(50, ge=1, le=500),
    _admin: dict[str, Any] = Depends(require_admin_job_permission("push:write")),
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
    _admin: dict[str, Any] = Depends(require_admin_permission("push:write")),
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
    _admin: dict[str, Any] = Depends(require_admin_permission("push:write")),
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
    _admin: dict[str, Any] = Depends(require_admin_permission("admin:read")),
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


@router.get("/audience")
async def get_push_audience_counts(
    _admin: dict[str, Any] = Depends(require_admin_permission("admin:read")),
) -> dict:
    try:
        return PriceAlertPushService().audience_counts()
    except PushNotificationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "push_audience_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.post("/broadcast")
async def send_broadcast_push_notification(
    payload: BroadcastRequest | None = Body(default=None),
    segment: str | None = Query(default=None, pattern=SEGMENT_PATTERN),
    title: str | None = Query(default=None, min_length=1, max_length=TITLE_MAX),
    body: str | None = Query(default=None, min_length=1, max_length=BODY_MAX),
    dry_run: bool = Query(True, alias="dryRun"),
    _admin: dict[str, Any] = Depends(require_admin_permission("push:write")),
) -> dict:
    resolved_segment = _resolve_required(
        payload.segment if payload else None, segment, "segment"
    )
    resolved_title = _resolve_required(payload.title if payload else None, title, "title")
    resolved_body = _resolve_required(payload.body if payload else None, body, "body")
    try:
        summary = PriceAlertPushService().dispatch_broadcast(
            segment=resolved_segment,
            title=resolved_title,
            body=resolved_body,
            dry_run=dry_run,
        )
    except PushNotificationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "push_broadcast_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error
    return summary.to_dict()


@router.post("/users/{user_id}/send")
async def send_push_to_user(
    user_id: str,
    payload: DirectSendRequest | None = Body(default=None),
    title: str | None = Query(default=None, min_length=1, max_length=TITLE_MAX),
    body: str | None = Query(default=None, min_length=1, max_length=BODY_MAX),
    device_id: str | None = Query(None, alias="deviceId"),
    dry_run: bool = Query(True, alias="dryRun"),
    _admin: dict[str, Any] = Depends(require_admin_permission("push:write")),
) -> dict:
    resolved_title = _resolve_required(payload.title if payload else None, title, "title")
    resolved_body = _resolve_required(payload.body if payload else None, body, "body")
    resolved_device_id = (payload.deviceId if payload else None) or device_id
    try:
        summary = PriceAlertPushService().dispatch_to_user(
            user_id=user_id,
            title=resolved_title,
            body=resolved_body,
            device_id=resolved_device_id,
            dry_run=dry_run,
        )
    except PushNotificationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "push_direct_send_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error
    return summary.to_dict()


@router.post("/devices/{device_id}/disable")
async def disable_push_device_registration(
    device_id: str,
    _admin: dict[str, Any] = Depends(require_admin_permission("push:write")),
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
