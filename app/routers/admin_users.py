from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.routers.admin_auth import require_admin_import_token, require_admin_permission
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_user_service import AdminUserService, AdminUserServiceError


router = APIRouter(prefix="/admin/users", tags=["Admin Users"])


class UserSupportActionRequest(BaseModel):
    email: str | None = Field(default=None, max_length=320)


class AdminRoleUpdateRequest(BaseModel):
    role: str = Field(pattern="^(admin|support|viewer|pricing_reviewer|user)$")
    isAdmin: bool = False


class AdminSubscriptionOverrideRequest(BaseModel):
    plan: str = Field(pattern="^(free|pro|premium)$")


@router.get("/{user_id}")
def get_admin_user_detail(
    user_id: str,
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminUserService().get_user_detail(user_id)
        _record_audit(
            action="admin_users.detail_viewed",
            status="success",
            metadata={"userId": user_id},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            action="admin_users.detail_viewed",
            status="failure",
            metadata={"userId": user_id, "error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_user_detail_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.post("/{user_id}/support/{action}")
def run_user_support_action(
    user_id: str,
    action: str,
    request: UserSupportActionRequest | None = None,
    admin: dict[str, Any] = Depends(require_admin_permission("users:write")),
) -> dict[str, Any]:
    if action not in {"resend_confirmation", "force_logout", "disable", "enable"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unsupported_user_support_action",
                "message": "Unsupported user support action.",
                "retryable": False,
            },
        )
    try:
        if action in {"disable", "force_logout"} and _is_self_target(admin, user_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "unsafe_self_admin_action",
                    "message": "You cannot disable or force logout your own admin session.",
                    "retryable": False,
                },
            )
        service = AdminUserService()
        if action == "resend_confirmation":
            email = str(request.email if request else "").strip().lower()
            if not email:
                raise AdminUserServiceError("Email is required to resend confirmation.")
            payload = service.resend_confirmation(email=email)
        elif action == "force_logout":
            payload = service.force_logout(user_id)
        elif action == "disable":
            payload = service.disable_user(user_id)
        else:
            payload = service.enable_user(user_id)
        _record_audit(
            action=f"admin_users.support.{action}",
            status="success",
            target_id=user_id,
            metadata={"email": str(request.email) if request and request.email else None},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            action=f"admin_users.support.{action}",
            status="failure",
            target_id=user_id,
            metadata={"error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_user_support_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.patch("/{user_id}/role")
def update_admin_user_role(
    user_id: str,
    request: AdminRoleUpdateRequest,
    admin: dict[str, Any] = Depends(require_admin_permission("users:write")),
) -> dict[str, Any]:
    try:
        if _is_self_target(admin, user_id) and not request.isAdmin:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "unsafe_self_admin_action",
                    "message": "You cannot remove admin access from your own active session.",
                    "retryable": False,
                },
            )
        payload = AdminUserService().update_admin_role(
            user_id=user_id,
            role=request.role,
            is_admin=request.isAdmin,
        )
        _record_audit(
            action="admin_users.role_updated",
            status="success",
            target_id=user_id,
            metadata={"role": request.role, "isAdmin": request.isAdmin},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            action="admin_users.role_updated",
            status="failure",
            target_id=user_id,
            metadata={"role": request.role, "isAdmin": request.isAdmin, "error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_role_update_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.post("/{user_id}/subscription")
def override_user_subscription(
    user_id: str,
    request: AdminSubscriptionOverrideRequest,
    admin: dict[str, Any] = Depends(require_admin_permission("users:write")),
) -> dict[str, Any]:
    try:
        payload = AdminUserService().override_subscription(user_id=user_id, plan=request.plan)
        _record_audit(
            action="admin_users.subscription_overridden",
            status="success",
            target_id=user_id,
            metadata={"plan": request.plan},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            action="admin_users.subscription_overridden",
            status="failure",
            target_id=user_id,
            metadata={"plan": request.plan, "error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_subscription_override_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.post("/{user_id}/scan-usage/reset")
def reset_user_scan_usage(
    user_id: str,
    admin: dict[str, Any] = Depends(require_admin_permission("users:write")),
) -> dict[str, Any]:
    try:
        payload = AdminUserService().reset_scan_usage(user_id)
        _record_audit(
            action="admin_users.scan_usage_reset",
            status="success",
            target_id=user_id,
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            action="admin_users.scan_usage_reset",
            status="failure",
            target_id=user_id,
            metadata={"error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_scan_usage_reset_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.get("")
def list_admin_users(
    q: str | None = Query(default=None, min_length=1),
    limit: int = Query(50, ge=1, le=100),
    _admin: dict[str, Any] = Depends(require_admin_import_token),
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
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        AdminAuditService().record(
            action=action,
            status=status,
            target_type="user" if target_id else None,
            target_id=target_id,
            metadata=metadata,
        )
    except Exception:
        return


def _is_self_target(admin: dict[str, Any], user_id: str) -> bool:
    admin_id = str(admin.get("id") or "").strip()
    return bool(admin_id and admin_id == user_id)
