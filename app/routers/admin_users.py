from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.routers.admin_auth import require_admin_import_token
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_user_service import AdminUserService, AdminUserServiceError


router = APIRouter(prefix="/admin/users", tags=["Admin Users"])


@router.get("")
def list_admin_users(
    q: str | None = Query(default=None, min_length=1),
    limit: int = Query(50, ge=1, le=100),
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    action = "admin_users.searched" if q else "admin_users.viewed"
    try:
        payload = AdminUserService().list_users(query=q, limit=limit)
        _record_audit(
            action=action,
            status="success",
            metadata={"query": q or "", "count": payload.get("count", 0)},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            action=action,
            status="failure",
            metadata={"query": q or "", "error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_users_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


def _record_audit(
    *,
    action: str,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        AdminAuditService().record(
            action=action,
            status=status,
            metadata=metadata,
        )
    except Exception:
        return
