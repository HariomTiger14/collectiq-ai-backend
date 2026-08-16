from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.routers.admin_auth import (
    require_admin_import_token,
    require_admin_job_token,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.pricing.batch_repricing_service import BatchRepricingService
from app.services.pricing.admin_review_queue_service import (
    AdminPricingReviewQueueService,
    ReviewQueueRepositoryError,
    ReviewQueueItemNotFoundError,
    ReviewQueueItemNotPriceableError,
)
from app.services.pricing.admin_health_service import PricingHealthService
from app.services.pricing.admin_health_service import PricingHealthError


router = APIRouter(prefix="/admin/pricing", tags=["Admin Pricing"])


class PricingOverrideRequest(BaseModel):
    estimatedValue: float = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    note: str | None = Field(default=None, max_length=1000)


class PricingAssignmentRequest(BaseModel):
    assignee: str | None = Field(default=None, max_length=120)
    status: str = Field(default="open", pattern="^(open|in_review|done)$")


class BulkPricingActionRequest(BaseModel):
    itemIds: list[str] = Field(min_length=1, max_length=100)


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


@router.get("/health/quick")
def pricing_health_quick(
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    """Providers + currency only, no catalog RPC -- see
    PricingHealthService.quick_health for why this exists separately from
    the full /health."""
    return PricingHealthService().quick_health()


@router.post("/reprice-all")
def reprice_all_portfolio_items(
    dry_run: bool = Query(False, alias="dryRun"),
    limit: int = Query(1000, ge=1, le=10000),
    _admin: None = Depends(require_admin_job_token),
) -> dict[str, Any]:
    """Scheduled batch re-pricing: re-value every portfolio item and persist
    refreshed values. Unavailable results are a no-op (never zero out a value).
    """
    summary = BatchRepricingService().reprice_all(limit=limit, dry_run=dry_run)
    payload = summary.to_dict()
    _record_audit(
        action="pricing.reprice_all",
        status="success",
        metadata={
            "scanned": payload["scanned"],
            "repriced": payload["repriced"],
            "unavailable": payload["unavailable"],
            "dryRun": dry_run,
        },
    )
    return payload


@router.get("/review-queue")
def pricing_review_queue(
    reason: str = Query(
        "all",
        pattern="^(all|needs_review|low_confidence|missing_price|stale_price)$",
    ),
    limit: int = Query(50, ge=1, le=200),
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminPricingReviewQueueService().list_queue(reason=reason, limit=limit)
        _record_audit(
            action="pricing_review_queue.viewed",
            status="success",
            metadata={"filter": reason, "count": payload.get("count", 0)},
        )
        return payload
    except ReviewQueueRepositoryError as error:
        _record_audit(
            action="pricing_review_queue.viewed",
            status="failure",
            metadata={"filter": reason, "error": str(error)},
        )
        raise _review_queue_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "review_queue_unavailable",
            str(error),
            retryable=True,
        ) from error


@router.post("/review-queue/{item_id}/reviewed")
def mark_pricing_reviewed(
    item_id: str,
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminPricingReviewQueueService().mark_reviewed(item_id)
        _record_audit(
            action="pricing_review_queue.mark_reviewed",
            status="success",
            target_id=item_id,
        )
        return payload
    except ReviewQueueItemNotFoundError as error:
        _record_audit(
            action="pricing_review_queue.mark_reviewed",
            status="failure",
            target_id=item_id,
            metadata={"error": str(error)},
        )
        raise _review_queue_error(
            status.HTTP_404_NOT_FOUND,
            "review_item_not_found",
            str(error),
        ) from error

    except ReviewQueueRepositoryError as error:
        _record_audit(
            action="pricing_review_queue.mark_reviewed",
            status="failure",
            target_id=item_id,
            metadata={"error": str(error)},
        )
        raise _review_queue_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "review_queue_unavailable",
            str(error),
            retryable=True,
        ) from error


@router.post("/review-queue/{item_id}/override")
def override_review_queue_price(
    item_id: str,
    request: PricingOverrideRequest,
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminPricingReviewQueueService().override_price(
            item_id,
            estimated_value=request.estimatedValue,
            currency=request.currency,
            note=request.note,
        )
        _record_audit(
            action="pricing_review_queue.override_price",
            status="success",
            target_id=item_id,
            metadata={
                "estimatedValue": request.estimatedValue,
                "currency": request.currency,
                "hasNote": bool(request.note),
            },
        )
        return payload
    except ReviewQueueItemNotFoundError as error:
        _record_audit(
            action="pricing_review_queue.override_price",
            status="failure",
            target_id=item_id,
            metadata={"error": str(error)},
        )
        raise _review_queue_error(
            status.HTTP_404_NOT_FOUND,
            "review_item_not_found",
            str(error),
        ) from error
    except ReviewQueueRepositoryError as error:
        _record_audit(
            action="pricing_review_queue.override_price",
            status="failure",
            target_id=item_id,
            metadata={"error": str(error)},
        )
        raise _review_queue_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "review_queue_unavailable",
            str(error),
            retryable=True,
        ) from error


@router.patch("/review-queue/{item_id}/assignment")
def assign_pricing_review_item(
    item_id: str,
    request: PricingAssignmentRequest,
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminPricingReviewQueueService().assign_item(
            item_id,
            assignee=request.assignee,
            status=request.status,
        )
        _record_audit(
            action="pricing_review_queue.assign",
            status="success",
            target_id=item_id,
            metadata={
                "assignee": request.assignee,
                "assignmentStatus": request.status,
            },
        )
        return payload
    except ReviewQueueItemNotFoundError as error:
        _record_audit(
            action="pricing_review_queue.assign",
            status="failure",
            target_id=item_id,
            metadata={"error": str(error)},
        )
        raise _review_queue_error(
            status.HTTP_404_NOT_FOUND,
            "review_item_not_found",
            str(error),
        ) from error
    except ReviewQueueRepositoryError as error:
        _record_audit(
            action="pricing_review_queue.assign",
            status="failure",
            target_id=item_id,
            metadata={"error": str(error)},
        )
        raise _review_queue_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "review_queue_unavailable",
            str(error),
            retryable=True,
        ) from error


@router.post("/review-queue/{item_id}/retry")
def retry_review_queue_pricing(
    item_id: str,
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminPricingReviewQueueService().retry_pricing(item_id)
        _record_audit(
            action="pricing_review_queue.retry_pricing",
            status="success",
            target_id=item_id,
            metadata={
                "pricingStatus": payload.get("pricing", {}).get("status"),
                "pricingConfidence": payload.get("pricing", {}).get("pricingConfidence"),
            },
        )
        return payload
    except ReviewQueueItemNotFoundError as error:
        _record_audit(
            action="pricing_review_queue.retry_pricing",
            status="failure",
            target_id=item_id,
            metadata={"error": str(error)},
        )
        raise _review_queue_error(
            status.HTTP_404_NOT_FOUND,
            "review_item_not_found",
            str(error),
        ) from error

    except ReviewQueueItemNotPriceableError as error:
        _record_audit(
            action="pricing_review_queue.retry_pricing",
            status="failure",
            target_id=item_id,
            metadata={"error": str(error)},
        )
        raise _review_queue_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "review_item_not_priceable",
            str(error),
        ) from error
    except ReviewQueueRepositoryError as error:
        _record_audit(
            action="pricing_review_queue.retry_pricing",
            status="failure",
            target_id=item_id,
            metadata={"error": str(error)},
        )
        raise _review_queue_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "review_queue_unavailable",
            str(error),
            retryable=True,
        ) from error


@router.post("/review-queue-actions/reviewed")
def bulk_mark_pricing_reviewed(
    request: BulkPricingActionRequest,
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    payload = AdminPricingReviewQueueService().bulk_mark_reviewed(request.itemIds)
    _record_audit(
        action="pricing_review_queue.bulk_mark_reviewed",
        status="success" if payload.get("success") else "failure",
        metadata={
            "requested": payload.get("requested"),
            "completed": payload.get("completed"),
            "failed": payload.get("failed"),
        },
    )
    return payload


@router.post("/review-queue-actions/retry")
def bulk_retry_review_queue_pricing(
    request: BulkPricingActionRequest,
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    payload = AdminPricingReviewQueueService().bulk_retry_pricing(request.itemIds)
    _record_audit(
        action="pricing_review_queue.bulk_retry_pricing",
        status="success" if payload.get("success") else "failure",
        metadata={
            "requested": payload.get("requested"),
            "completed": payload.get("completed"),
            "failed": payload.get("failed"),
        },
    )
    return payload


def _review_queue_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    )


def _record_audit(
    *,
    action: str,
    status: str,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        AdminAuditService().record(
            action=action,
            status=status,
            target_type="portfolio_item" if target_id else None,
            target_id=target_id,
            metadata=metadata,
        )
    except Exception:
        return
