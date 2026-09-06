from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.routers.admin_auth import (
    require_admin_import_token,
    require_admin_permission,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_catalog_service import AdminCatalogError, AdminCatalogService
from app.services.admin_pipeline_status_service import (
    AdminPipelineStatusError,
    AdminPipelineStatusService,
)


router = APIRouter(prefix="/admin/catalog", tags=["Admin Catalog"])


@router.get("/pipelines")
def get_catalog_pipeline_status(
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return AdminPipelineStatusService().get_summary()
    except AdminPipelineStatusError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_pipeline_status_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.get("/items")
def list_catalog_items(
    source: str = Query(default="pricecharting", pattern="^(pricecharting|kicksdb|all)$"),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    category: str | None = Query(default=None, min_length=1),
    categoryGroup: str | None = Query(default=None, min_length=1),
    minPrice: float | None = Query(default=None, ge=0),
    maxPrice: float | None = Query(default=None, ge=0),
    q: str | None = Query(default=None, max_length=120),
    sort: str | None = Query(default=None, pattern="^(price_asc|price_desc)$"),
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return AdminCatalogService().list_items(
            source=source, limit=limit, offset=offset,
            category=category, category_group=categoryGroup, min_price=minPrice, max_price=maxPrice,
            query=q, sort=sort,
        )
    except AdminCatalogError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_catalog_list_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


class CatalogUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=160)
    console: str | None = Field(default=None, max_length=160)
    upc: str | None = Field(default=None, max_length=80)
    productUrl: str | None = Field(default=None, max_length=1000)
    note: str | None = Field(default=None, max_length=1000)
    active: bool | None = None


@router.patch("/{catalog_id}")
def update_catalog_item(
    catalog_id: str,
    request: CatalogUpdateRequest,
    _admin: dict[str, Any] = Depends(require_admin_permission("catalog:write")),
) -> dict[str, Any]:
    try:
        payload = AdminCatalogService().update_item(
            catalog_id,
            request.model_dump(exclude_unset=True),
        )
        _record_audit(
            "admin_catalog.item_updated",
            "success",
            catalog_id,
            {"fields": sorted(request.model_dump(exclude_unset=True).keys())},
            admin=_admin,
        )
        return payload
    except AdminCatalogError as error:
        _record_audit(
            "admin_catalog.item_updated",
            "failure",
            catalog_id,
            {"error": str(error)},
            admin=_admin,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_catalog_update_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


def _record_audit(
    action: str,
    event_status: str,
    target_id: str,
    metadata: dict[str, Any],
    admin: dict[str, Any] | None = None,
) -> None:
    # Reuses admin_users.py's convention verbatim so the audit log has one
    # actor format. Correct for both identities without branching: a Supabase
    # session carries an email, while _static_admin() has id="admin_token" and
    # no email, so a runbook or cron action still records admin_token -- but
    # accurately, rather than because the argument was omitted.
    try:
        AdminAuditService().record(
            actor=str((admin or {}).get("email") or (admin or {}).get("id") or "admin_token"),
            action=action,
            status=event_status,
            target_type="catalog_item",
            target_id=target_id,
            metadata=metadata,
        )
    except Exception:
        return
