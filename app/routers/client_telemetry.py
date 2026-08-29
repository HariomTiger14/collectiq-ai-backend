"""App-side error reporting into the shared ops_error_events feed.

The mobile app's global error hooks already exist (FlutterError.onError,
PlatformDispatcher.onError, runZonedGuarded -- see main.dart) and route
through its AppTelemetryService. The Firebase provider sends them to
Crashlytics for deep forensics; THIS endpoint is the second lane, landing
the same errors in ops_error_events so the admin portal's error feed
shows app failures next to API and cron ones -- one pane, not two tools.

Auth: the user's own Supabase bearer token, resolved the same way the
data-requests endpoints do it. That keeps the endpoint from being an
anonymous spam sink while never needing a new credential. A report is
best-effort telemetry: on any auth failure the app just drops it.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.core.config import settings

router = APIRouter(prefix="/api/ops", tags=["Client Telemetry"])

_STACK_CAP = 4000
_MESSAGE_CAP = 2000


class ClientErrorReport(BaseModel):
    errorClass: str = Field(min_length=1, max_length=120)
    message: str | None = Field(default=None, max_length=_MESSAGE_CAP)
    stack: str | None = Field(default=None, max_length=16000)
    # Free-form but small: screen, appVersion, platform, reason.
    context: dict[str, Any] = Field(default_factory=dict)


@router.post("/client-errors", status_code=status.HTTP_202_ACCEPTED)
def report_client_error(
    report: ClientErrorReport,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    user_id = _user_id_from_bearer(authorization)
    context = {
        key: str(value)[:200]
        for key, value in list(report.context.items())[:12]
        if isinstance(key, str)
    }
    context["userId"] = user_id
    # Fingerprint on class + the screen/reason the app tagged, NOT the
    # message -- same rule as the api/cron writers: messages embed values
    # and would shatter one bug into a thousand issues.
    scope = context.get("reason") or context.get("screen") or ""
    fingerprint = hashlib.md5(f"app|{scope}|{report.errorClass}".encode()).hexdigest()
    _insert_error_event(
        {
            "source": "app",
            "job_name": scope or None,
            "error_class": report.errorClass,
            "message": (report.message or "")[:_MESSAGE_CAP] or None,
            "stack": (report.stack or "")[-_STACK_CAP:] or None,
            "context": context,
            "fingerprint": fingerprint,
        }
    )
    return {"success": True}


def _user_id_from_bearer(authorization: str | None) -> str:
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail={"code": "missing_token"})
    supabase_url = (settings.supabase_url or "").strip().rstrip("/")
    anon_key = (getattr(settings, "supabase_anon_key", "") or settings.supabase_service_role_key or "").strip()
    if not supabase_url or not anon_key:
        raise HTTPException(status_code=503, detail={"code": "auth_unconfigured"})
    try:
        response = httpx.get(
            f"{supabase_url}/auth/v1/user",
            headers={"apikey": anon_key, "Authorization": f"Bearer {token}"},
            timeout=8,
        )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail={"code": "auth_unreachable"}) from error
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail={"code": "invalid_token"})
    user_id = (response.json() or {}).get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail={"code": "invalid_token"})
    return str(user_id)


def _insert_error_event(row: dict[str, Any]) -> None:
    supabase_url = (settings.supabase_url or "").strip().rstrip("/")
    service_key = (settings.supabase_service_role_key or "").strip()
    if not supabase_url or not service_key:
        return
    try:
        httpx.post(
            f"{supabase_url}/rest/v1/ops_error_events",
            json=row,
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            timeout=8,
        ).raise_for_status()
    except httpx.HTTPError:
        # Telemetry must never fail the report call loudly -- the app
        # treats this endpoint as fire-and-forget either way.
        pass
