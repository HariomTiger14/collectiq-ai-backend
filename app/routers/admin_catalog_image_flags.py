from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.routers.admin_auth import require_admin_import_token, require_admin_permission
from app.services.admin_audit_service import AdminAuditService
from app.services.catalog_image_flags_service import (
    CatalogImageFlagsError,
    CatalogImageFlagsService,
    UnknownCatalogImageCategoryError,
)

router = APIRouter(prefix="/admin/catalog-image-flags", tags=["Admin Catalog Image Flags"])


class CatalogImageFlagUpdateRequest(BaseModel):
    enabled: bool


@router.get("")
def list_catalog_image_flags(
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return CatalogImageFlagsService().list_flags()
    except CatalogImageFlagsError as error:
        raise _flags_error(str(error)) from error


@router.patch("/{category}")
def update_catalog_image_flag(
    category: str,
    request: CatalogImageFlagUpdateRequest,
    admin: dict[str, Any] = Depends(require_admin_permission("catalog:write")),
) -> dict[str, Any]:
    actor = str(admin.get("email") or admin.get("id") or "admin")
    try:
        payload = CatalogImageFlagsService().set_flag(
            category=category, enabled=request.enabled
        )
        _record_audit(
            "admin_catalog_image_flags.toggled",
            "success",
            category,
            actor,
            {"enabled": request.enabled},
        )
        return payload
    except UnknownCatalogImageCategoryError as error:
        _record_audit(
            "admin_catalog_image_flags.toggled",
            "failure",
            category,
            actor,
            {"error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "unknown_catalog_image_category", "message": str(error)},
        ) from error
    except CatalogImageFlagsError as error:
        _record_audit(
            "admin_catalog_image_flags.toggled",
            "failure",
            category,
            actor,
            {"error": str(error)},
        )
        raise _flags_error(str(error)) from error


def _flags_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "catalog_image_flags_unavailable",
            "message": message,
            "retryable": True,
        },
    )


def _record_audit(
    action: str,
    event_status: str,
    target_id: str,
    actor: str,
    metadata: dict[str, Any],
) -> None:
    try:
        AdminAuditService().record(
            action=action,
            status=event_status,
            target_type="catalog_image_flag",
            target_id=target_id,
            actor=actor,
            metadata=metadata,
        )
    except Exception:
        return
