from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.routers.admin_auth import require_admin_import_token, require_admin_permission
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_subscription_metrics_service import (
    AdminSubscriptionMetricsError,
    AdminSubscriptionMetricsService,
)
from app.services.admin_user_service import AdminUserService, AdminUserServiceError


router = APIRouter(prefix="/admin/users", tags=["Admin Users"])


class UserSupportActionRequest(BaseModel):
    email: str | None = Field(default=None, max_length=320)


class AdminRoleUpdateRequest(BaseModel):
    role: str = Field(pattern="^(admin|support|viewer|pricing_reviewer|user)$")
    isAdmin: bool = False


class AdminSubscriptionOverrideRequest(BaseModel):
    plan: str = Field(pattern="^(free|pro|premium)$")


class CollectorProfileUpdateRequest(BaseModel):
    displayName: str | None = Field(default=None, max_length=200)
    countryCode: str | None = Field(default=None, max_length=8)
    preferredCurrency: str | None = Field(default=None, max_length=8)


class WishlistEntryUpdateRequest(BaseModel):
    status: str = Field(pattern="^(owned|wanted|missing)$")


class PriceAlertUpdateRequest(BaseModel):
    enabled: bool | None = None
    status: str | None = Field(default=None, pattern="^(active|triggered|notified|paused)$")
    targetAmount: float | None = None
    percentage: float | None = None
    message: str | None = Field(default=None, max_length=500)


class TeamInviteRequest(BaseModel):
    email: str = Field(max_length=320)
    # No "user" here (unlike AdminRoleUpdateRequest) -- inviting someone
    # onto the team with the default no-access role would be a pointless
    # invite email.
    role: str = Field(pattern="^(admin|support|viewer|pricing_reviewer)$")
    isAdmin: bool = False


# Registered before /{user_id} below -- FastAPI matches path routes in
# registration order, so /team must come first or "/admin/users/team"
# would be swallowed by get_admin_user_detail with user_id="team" (same
# reason /subscription-summary above is also ordered ahead of it).
@router.get("/team")
def list_admin_team(
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminUserService().list_team()
        _record_audit(admin=_admin, action="admin_users.team_viewed", status="success")
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=_admin,
            action="admin_users.team_viewed",
            status="failure",
            metadata={"error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_team_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.post("/team")
def invite_admin_team_member(
    request: TeamInviteRequest,
    admin: dict[str, Any] = Depends(require_admin_permission("users:write")),
) -> dict[str, Any]:
    email = request.email.strip().lower()
    try:
        payload = AdminUserService().invite_team_member(
            email=email, role=request.role, is_admin=request.isAdmin,
        )
        _record_audit(
            admin=admin,
            action="admin_users.team_invited",
            status="success",
            target_id=payload.get("userId"),
            metadata={"email": email, "role": request.role, "isAdmin": request.isAdmin},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=admin,
            action="admin_users.team_invited",
            status="failure",
            metadata={"email": email, "role": request.role, "error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_team_invite_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.get("/subscription-summary")
def get_admin_subscription_summary(
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return AdminSubscriptionMetricsService().get_summary()
    except AdminSubscriptionMetricsError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_subscription_summary_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.get("/{user_id}")
def get_admin_user_detail(
    user_id: str,
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminUserService().get_user_detail(user_id)
        _record_audit(
            admin=_admin,
            action="admin_users.detail_viewed",
            status="success",
            metadata={"userId": user_id},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=_admin,
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
            admin=admin,
            action=f"admin_users.support.{action}",
            status="success",
            target_id=user_id,
            metadata={"email": str(request.email) if request and request.email else None},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=admin,
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
            admin=admin,
            action="admin_users.role_updated",
            status="success",
            target_id=user_id,
            metadata={"role": request.role, "isAdmin": request.isAdmin},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=admin,
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
            admin=admin,
            action="admin_users.subscription_overridden",
            status="success",
            target_id=user_id,
            metadata={"plan": request.plan},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=admin,
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
            admin=admin,
            action="admin_users.scan_usage_reset",
            status="success",
            target_id=user_id,
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=admin,
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


@router.patch("/{user_id}/collector-profile")
def update_user_collector_profile(
    user_id: str,
    request: CollectorProfileUpdateRequest,
    admin: dict[str, Any] = Depends(require_admin_permission("users:write")),
) -> dict[str, Any]:
    try:
        payload = AdminUserService().update_collector_profile(
            user_id=user_id,
            display_name=request.displayName,
            country_code=request.countryCode,
            preferred_currency=request.preferredCurrency,
        )
        _record_audit(
            admin=admin,
            action="admin_users.collector_profile_updated",
            status="success",
            target_id=user_id,
            metadata=request.model_dump(exclude_none=True),
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=admin,
            action="admin_users.collector_profile_updated",
            status="failure",
            target_id=user_id,
            metadata={"error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_collector_profile_update_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.patch("/{user_id}/wishlist/{item_id}")
def update_user_wishlist_entry(
    user_id: str,
    item_id: str,
    request: WishlistEntryUpdateRequest,
    admin: dict[str, Any] = Depends(require_admin_permission("users:write")),
) -> dict[str, Any]:
    try:
        payload = AdminUserService().update_wishlist_entry(
            user_id=user_id, item_id=item_id, status=request.status
        )
        _record_audit(
            admin=admin,
            action="admin_users.wishlist_entry_updated",
            status="success",
            target_id=user_id,
            metadata={"itemId": item_id, "status": request.status},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=admin,
            action="admin_users.wishlist_entry_updated",
            status="failure",
            target_id=user_id,
            metadata={"itemId": item_id, "error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_wishlist_update_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.delete("/{user_id}/wishlist/{item_id}")
def delete_user_wishlist_entry(
    user_id: str,
    item_id: str,
    admin: dict[str, Any] = Depends(require_admin_permission("users:write")),
) -> dict[str, Any]:
    try:
        payload = AdminUserService().delete_wishlist_entry(user_id=user_id, item_id=item_id)
        _record_audit(
            admin=admin,
            action="admin_users.wishlist_entry_deleted",
            status="success",
            target_id=user_id,
            metadata={"itemId": item_id},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=admin,
            action="admin_users.wishlist_entry_deleted",
            status="failure",
            target_id=user_id,
            metadata={"itemId": item_id, "error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_wishlist_delete_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.patch("/{user_id}/price-alerts/{alert_id}")
def update_user_price_alert(
    user_id: str,
    alert_id: str,
    request: PriceAlertUpdateRequest,
    admin: dict[str, Any] = Depends(require_admin_permission("users:write")),
) -> dict[str, Any]:
    fields = {
        "enabled": request.enabled,
        "status": request.status,
        "target_amount": request.targetAmount,
        "percentage": request.percentage,
        "message": request.message,
    }
    try:
        payload = AdminUserService().update_price_alert(user_id=user_id, alert_id=alert_id, fields=fields)
        _record_audit(
            admin=admin,
            action="admin_users.price_alert_updated",
            status="success",
            target_id=user_id,
            metadata={"alertId": alert_id, **request.model_dump(exclude_none=True)},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=admin,
            action="admin_users.price_alert_updated",
            status="failure",
            target_id=user_id,
            metadata={"alertId": alert_id, "error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_price_alert_update_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.delete("/{user_id}/price-alerts/{alert_id}")
def delete_user_price_alert(
    user_id: str,
    alert_id: str,
    admin: dict[str, Any] = Depends(require_admin_permission("users:write")),
) -> dict[str, Any]:
    try:
        payload = AdminUserService().delete_price_alert(user_id=user_id, alert_id=alert_id)
        _record_audit(
            admin=admin,
            action="admin_users.price_alert_deleted",
            status="success",
            target_id=user_id,
            metadata={"alertId": alert_id},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=admin,
            action="admin_users.price_alert_deleted",
            status="failure",
            target_id=user_id,
            metadata={"alertId": alert_id, "error": str(error)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_price_alert_delete_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.get("")
def list_admin_users(
    q: str | None = Query(default=None, min_length=1),
    limit: int = Query(50, ge=1, le=100),
    page: int = Query(1, ge=1),
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    action = "admin_users.searched" if q else "admin_users.viewed"
    try:
        payload = AdminUserService().list_users(query=q, limit=limit, page=page)
        _record_audit(
            admin=_admin,
            action=action,
            status="success",
            metadata={"query": q or "", "count": payload.get("count", 0), "page": page},
        )
        return payload
    except AdminUserServiceError as error:
        _record_audit(
            admin=_admin,
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
    admin: dict[str, Any],
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
            # Every call site here used to omit actor entirely, so every
            # role change / support action / etc. landed with the generic
            # default "admin_token" regardless of who actually did it --
            # "last admin action by this person" (the Team & access roster)
            # depends on this being the real acting admin's identity.
            actor=str(admin.get("email") or admin.get("id") or "admin_token"),
        )
    except Exception:
        return


def _is_self_target(admin: dict[str, Any], user_id: str) -> bool:
    admin_id = str(admin.get("id") or "").strip()
    return bool(admin_id and admin_id == user_id)
