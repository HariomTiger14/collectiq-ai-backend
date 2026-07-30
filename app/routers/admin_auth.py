import httpx
from fastapi import Header, HTTPException, status

from app.core.config import settings


def require_admin_import_token(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    authorization: str | None = Header(default=None),
) -> None:
    _require_admin_token(
        expected_token=settings.admin_import_token,
        not_configured_code="admin_import_not_configured",
        not_configured_message="ADMIN_IMPORT_TOKEN is not configured.",
        x_admin_token=x_admin_token,
        authorization=authorization,
    )


def require_admin_job_token(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    authorization: str | None = Header(default=None),
) -> None:
    _require_admin_token(
        expected_token=settings.admin_job_token,
        not_configured_code="admin_job_not_configured",
        not_configured_message="ADMIN_JOB_TOKEN is not configured.",
        x_admin_token=x_admin_token,
        authorization=authorization,
    )


def require_admin_session(
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    return _require_supabase_admin(_bearer_token(authorization))


def _require_admin_token(
    *,
    expected_token: str,
    not_configured_code: str,
    not_configured_message: str,
    x_admin_token: str | None,
    authorization: str | None,
) -> None:
    supplied_token = (x_admin_token or _bearer_token(authorization) or "").strip()
    expected = expected_token.strip()
    if expected and supplied_token == expected:
        return

    if _is_supabase_admin_token(supplied_token):
        return

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


def _is_supabase_admin_token(token: str) -> bool:
    try:
        _require_supabase_admin(token)
    except HTTPException:
        return False
    return True


def _require_supabase_admin(token: str) -> dict[str, str]:
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
    email = str(user.get("email") or "").strip().lower()
    if email not in _allowed_admin_emails():
        raise _unauthorized_admin_session()

    return {"id": str(user.get("id") or ""), "email": email}


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


def _supabase_admin_auth_configured() -> bool:
    return bool(
        _setting_str("supabase_url")
        and (_setting_str("supabase_anon_key") or _setting_str("supabase_service_role_key"))
        and _allowed_admin_emails()
    )


def _allowed_admin_emails() -> tuple[str, ...]:
    emails = getattr(settings, "admin_allowed_emails", ())
    if not isinstance(emails, (tuple, list, set)):
        return ()
    return tuple(str(email).strip().lower() for email in emails if str(email).strip())


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
