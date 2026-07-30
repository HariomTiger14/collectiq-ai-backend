from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.routers.admin_auth import require_admin_import_token
from app.services.pricing.admin_review_queue_service import (
    AdminPricingReviewQueueService,
    ReviewQueueItemNotFoundError,
    ReviewQueueItemNotPriceableError,
)
from app.services.pricing.admin_health_service import PricingHealthService
from app.services.pricing.admin_health_service import PricingHealthError


router = APIRouter(prefix="/admin/pricing", tags=["Admin Pricing"])


@router.get("/health")
def pricing_health(
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return PricingHealthService().health()
    except PricingHealthError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "pricing_health_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.get("/review-queue")
def pricing_review_queue(
    reason: str = Query(
        "all",
        pattern="^(all|needs_review|low_confidence|missing_price|stale_price)$",
    ),
    limit: int = Query(50, ge=1, le=200),
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    return AdminPricingReviewQueueService().list_queue(reason=reason, limit=limit)


@router.post("/review-queue/{item_id}/reviewed")
def mark_pricing_reviewed(
    item_id: str,
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return AdminPricingReviewQueueService().mark_reviewed(item_id)
    except ReviewQueueItemNotFoundError as error:
        raise _review_queue_error(
            status.HTTP_404_NOT_FOUND,
            "review_item_not_found",
            str(error),
        ) from error


@router.post("/review-queue/{item_id}/retry")
def retry_review_queue_pricing(
    item_id: str,
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return AdminPricingReviewQueueService().retry_pricing(item_id)
    except ReviewQueueItemNotFoundError as error:
        raise _review_queue_error(
            status.HTTP_404_NOT_FOUND,
            "review_item_not_found",
            str(error),
        ) from error
    except ReviewQueueItemNotPriceableError as error:
        raise _review_queue_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "review_item_not_priceable",
            str(error),
        ) from error


def _review_queue_error(
    status_code: int,
    code: str,
    message: str,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": False,
        },
    )
