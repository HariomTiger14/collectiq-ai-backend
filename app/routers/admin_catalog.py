from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.routers.admin_auth import require_admin_import_token
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_catalog_service import AdminCatalogError, AdminCatalogService
from app.services.admin_pipeline_status_service import (
    AdminPipelineStatusError,
    AdminPipelineStatusService,
)


router = APIRouter(prefix="/admin/catalog", tags=["Admin Catalog"])


@router.get("/pipelines")
def get_catalog_pipeline_status(
    _admin: None = Depends(require_admin_import_token),
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
    source: str = Query(default="pricecharting", pattern="^(pricecharting|kicksdb)$"),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    category: str | None = Query(default=None, min_length=1),
    categoryGroup: str | None = Query(default=None, min_length=1),
    minPrice: float | None = Query(default=None, ge=0),
    maxPrice: float | None = Query(default=None, ge=0),
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return AdminCatalogService().list_items(
            source=source, limit=limit, offset=offset,
            category=category, category_group=categoryGroup, min_price=minPrice, max_price=maxPrice,
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
    _admin: None = Depends(require_admin_import_token),
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
        )
        return payload
    except AdminCatalogError as error:
        _record_audit(
            "admin_catalog.item_updated",
            "failure",
            catalog_id,
            {"error": str(error)},
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
) -> None:
    try:
        AdminAuditService().record(
            action=action,
            status=event_status,
            target_type="catalog_item",
            target_id=target_id,
            metadata=metadata,
        )
    except Exception:
        return
