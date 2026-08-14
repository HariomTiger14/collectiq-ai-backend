from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.routers.admin_auth import require_admin_import_token
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_portfolio_service import AdminPortfolioService


router = APIRouter(prefix="/admin/portfolio", tags=["Admin Portfolio"])


class PortfolioItemUpdateRequest(BaseModel):
    # Deliberately excludes price/currency/confidence/pricingProvider/
    # valuationStatus/reviewStatus. Those are financial/workflow fields that
    # already have their own properly-audited override paths — pricing
    # overrides go through the Pricing Review Queue (typed confirmation +
    # mandatory note), and reviewStatus is written by the scan-failure and
    # review-queue flows. Letting this general-purpose item edit form also
    # write those fields, with no note requirement, was a second, looser
    # door to the same consequential actions.
    category: str | None = Field(default=None, max_length=120)
    condition: str | None = Field(default=None, max_length=120)
    adminNotes: str | None = Field(default=None, max_length=2000)


@router.get("/items")
def list_admin_portfolio_items(
    q: str | None = Query(default=None, min_length=1),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    userId: str | None = Query(default=None, min_length=1),
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    payload = AdminPortfolioService().list_items(query=q, limit=limit, user_id=userId, offset=offset)
    _record_audit(
        action="admin_portfolio.items_viewed",
        status="success",
        metadata={"query": q or "", "userId": userId or "", "offset": offset, "count": payload.get("count", 0)},
    )
    return payload


@router.get("/items/{item_id}")
def get_admin_portfolio_item(
    item_id: str,
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminPortfolioService().get_item(item_id)
        _record_audit(
            action="admin_portfolio.item_viewed",
            status="success",
            target_id=item_id,
        )
        return payload
    except KeyError as error:
        _record_audit(
            action="admin_portfolio.item_viewed",
            status="failure",
            target_id=item_id,
            metadata={"error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "portfolio_item_not_found",
                "message": str(error),
                "retryable": False,
            },
        ) from error


@router.patch("/items/{item_id}")
def update_admin_portfolio_item(
    item_id: str,
    request: PortfolioItemUpdateRequest,
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminPortfolioService().update_item(
            item_id,
            request.model_dump(exclude_unset=True),
            actor=str(_admin.get("email") or _admin.get("id") or "admin"),
        )
        _record_audit(
            action="admin_portfolio.item_updated",
            status="success",
            target_id=item_id,
            metadata={"updated": payload.get("updated"), "fields": list(request.model_dump(exclude_unset=True).keys())},
        )
        return payload
    except KeyError as error:
        _record_audit(
            action="admin_portfolio.item_updated",
            status="failure",
            target_id=item_id,
            metadata={"error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "portfolio_item_not_found",
                "message": str(error),
                "retryable": False,
            },
        ) from error


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
