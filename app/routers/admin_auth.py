from collections.abc import Callable
from typing import Any

import httpx
from fastapi import Header, HTTPException, status

from app.core.config import settings


FULL_ADMIN_PERMISSIONS = {
    "admin:read",
    "audit:read",
    "catalog:write",
    "imports:run",
    "pricing:write",
    "reports:export",
    "scans:write",
    "users:write",
}

ROLE_PERMISSIONS = {
    "viewer": {"admin:read", "audit:read"},
    "support": {"admin:read", "audit:read", "reports:export", "scans:write", "users:write"},
    "pricing_reviewer": {
        "admin:read",
        "audit:read",
        "catalog:write",
        "imports:run",
        "pricing:write",
        "reports:export",
    },
    "admin": FULL_ADMIN_PERMISSIONS,
    "owner": FULL_ADMIN_PERMISSIONS,
    "super_admin": FULL_ADMIN_PERMISSIONS,
}


def require_admin_import_token(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return _require_admin_token(
        expected_token=settings.admin_import_token,
        not_configured_code="admin_import_not_configured",
        not_configured_message="ADMIN_IMPORT_TOKEN is not configured.",
        x_admin_token=x_admin_token,
        authorization=authorization,
    )


def require_admin_job_token(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return _require_admin_token(
        expected_token=settings.admin_job_token,
        not_configured_code="admin_job_not_configured",
        not_configured_message="ADMIN_JOB_TOKEN is not configured.",
        x_admin_token=x_admin_token,
        authorization=authorization,
    )


def require_admin_session(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return _require_supabase_admin(_bearer_token(authorization))


def require_admin_permission(permission: str) -> Callable[..., dict[str, Any]]:
    def dependency(
        x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        admin = require_admin_import_token(
            x_admin_token=x_admin_token,
            authorization=authorization,
        )
        if permission in set(admin.get("permissions") or []):
            return admin
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_permission_denied",
                "message": "This admin role cannot perform that action.",
                "retryable": False,
            },
        )

    return dependency


def _require_admin_token(
    *,
    expected_token: str,
    not_configured_code: str,
    not_configured_message: str,
    x_admin_token: str | None,
    authorization: str | None,
) -> dict[str, Any]:
    supplied_token = (x_admin_token or _bearer_token(authorization) or "").strip()
    expected = expected_token.strip()
    if expected and supplied_token == expected:
        return _static_admin()

    supabase_admin = _supabase_admin_token(supplied_token)
    if supabase_admin:
        return supabase_admin

    if not expected and not _supabase_admin_auth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": not_configured_code,
                "message": not_configured_message,
                "retryable": False,
            },
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "unauthorized",
            "message": "Admin credentials are invalid.",
            "retryable": False,
        },
    )


def _supabase_admin_token(token: str) -> dict[str, Any] | None:
    try:
        return _require_supabase_admin(token)
    except HTTPException:
        return None


def _require_supabase_admin(token: str) -> dict[str, Any]:
    token = token.strip()
    if not token:
        raise _unauthorized_admin_session()
    if not _supabase_admin_auth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_auth_not_configured",
                "message": "Supabase admin authentication is not configured.",
                "retryable": False,
            },
        )

    user = _fetch_supabase_user(token)
    user_id = str(user.get("id") or "").strip()
    email = str(user.get("email") or "").strip().lower()
    profile = _fetch_admin_profile(user_id)
    if not _profile_has_admin_role(profile):
        raise _unauthorized_admin_session()

    role = str(profile.get("role") or profile.get("admin_role") or "admin").strip().lower()
    permissions = sorted(_permissions_for_role(role))
    return {
        "id": user_id,
        "email": email,
        "role": role,
        "isAdmin": True,
        "permissions": permissions,
        "canWrite": bool(set(permissions) - {"admin:read", "audit:read"}),
    }


def _fetch_supabase_user(token: str) -> dict:
    supabase_url = _setting_str("supabase_url").rstrip("/")
    auth_key = (_setting_str("supabase_anon_key") or _setting_str("supabase_service_role_key")).strip()
    try:
        response = httpx.get(
            f"{supabase_url}/auth/v1/user",
            headers={
                "apikey": auth_key,
                "Authorization": f"Bearer {token}",
            },
            timeout=5,
        )
    except httpx.HTTPError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_auth_unavailable",
                "message": "Admin authentication is temporarily unavailable.",
                "retryable": True,
            },
        )

    if response.status_code != 200:
        raise _unauthorized_admin_session()

    payload = response.json()
    if not isinstance(payload, dict):
        raise _unauthorized_admin_session()
    return payload


def _fetch_admin_profile(user_id: str) -> dict:
    if not user_id:
        raise _unauthorized_admin_session()
    supabase_url = _setting_str("supabase_url").rstrip("/")
    service_role_key = _setting_str("supabase_service_role_key")
    table = _admin_profile_table()
    try:
        response = httpx.get(
            f"{supabase_url}/rest/v1/{table}",
            headers={
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
            },
            params={"id": f"eq.{user_id}", "select": "*", "limit": "1"},
            timeout=5,
        )
    except httpx.HTTPError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_auth_unavailable",
                "message": "Admin authentication is temporarily unavailable.",
                "retryable": True,
            },
        )

    if response.status_code != 200:
        raise _unauthorized_admin_session()
    payload = response.json()
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise _unauthorized_admin_session()
    return payload[0]


def _profile_has_admin_role(profile: dict) -> bool:
    if profile.get("is_admin") is True:
        return True
    role = str(profile.get("role") or profile.get("admin_role") or "").strip().lower()
    return role in ROLE_PERMISSIONS and role != "user"


def _permissions_for_role(role: str) -> set[str]:
    normalized = (role or "viewer").strip().lower()
    return set(ROLE_PERMISSIONS.get(normalized, ROLE_PERMISSIONS["viewer"]))


def _static_admin() -> dict[str, Any]:
    return {
        "id": "admin_token",
        "email": "",
        "role": "admin",
        "isAdmin": True,
        "permissions": sorted(FULL_ADMIN_PERMISSIONS),
        "canWrite": True,
        "authMode": "admin_token",
    }


def _supabase_admin_auth_configured() -> bool:
    return bool(
        _setting_str("supabase_url")
        and (_setting_str("supabase_anon_key") or _setting_str("supabase_service_role_key"))
        and _setting_str("supabase_service_role_key")
        and _admin_profile_table()
    )


def _admin_profile_table() -> str:
    value = _setting_str("admin_profile_table")
    return value or "profiles"


def _setting_str(name: str) -> str:
    value = getattr(settings, name, "")
    return value.strip() if isinstance(value, str) else ""


def _unauthorized_admin_session() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "unauthorized",
            "message": "Admin credentials are invalid.",
            "retryable": False,
        },
    )


def _bearer_token(authorization: str | None) -> str:
    if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return ""
