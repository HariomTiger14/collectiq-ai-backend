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


def _require_admin_token(
    *,
    expected_token: str,
    not_configured_code: str,
    not_configured_message: str,
    x_admin_token: str | None,
    authorization: str | None,
) -> None:
    expected = expected_token.strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": not_configured_code,
                "message": not_configured_message,
                "retryable": False,
            },
        )

    supplied_token = (x_admin_token or _bearer_token(authorization) or "").strip()
    if supplied_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "unauthorized",
                "message": "Admin token is invalid.",
                "retryable": False,
            },
        )


def _bearer_token(authorization: str | None) -> str:
    if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return ""
