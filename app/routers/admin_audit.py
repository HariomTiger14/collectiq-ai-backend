from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.routers.admin_auth import require_admin_import_token
from app.services.admin_audit_service import AdminAuditError, AdminAuditService


router = APIRouter(prefix="/admin/audit", tags=["Admin Audit"])


@router.get("/events")
def list_admin_audit_events(
    action: str | None = Query(default=None, min_length=1),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(success|failure|unknown)$"),
    target_type: str | None = Query(default=None, alias="targetType", min_length=1),
    target_id: str | None = Query(default=None, alias="targetId", min_length=1),
    actor: str | None = Query(default=None, min_length=1),
    since: str | None = Query(default=None, min_length=1),
    until: str | None = Query(default=None, min_length=1),
    limit: int = Query(50, ge=1, le=200),
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return AdminAuditService().list_events(
            action=action,
            status=status_filter,
            target_type=target_type,
            target_id=target_id,
            actor=actor,
            since=since,
            until=until,
            limit=limit,
        )
    except AdminAuditError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_audit_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error
