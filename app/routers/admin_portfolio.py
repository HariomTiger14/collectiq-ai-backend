from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.routers.admin_auth import require_admin_import_token
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_portfolio_service import AdminPortfolioService


router = APIRouter(prefix="/admin/portfolio", tags=["Admin Portfolio"])


@router.get("/items")
def list_admin_portfolio_items(
    q: str | None = Query(default=None, min_length=1),
    limit: int = Query(50, ge=1, le=100),
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    payload = AdminPortfolioService().list_items(query=q, limit=limit)
    _record_audit(
        action="admin_portfolio.items_viewed",
        status="success",
        metadata={"query": q or "", "count": payload.get("count", 0)},
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
