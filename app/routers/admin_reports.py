from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.routers.admin_auth import require_admin_import_token
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_reports_service import AdminReportsService
from app.services.admin_scan_failure_service import AdminScanFailureService
from app.services.admin_user_service import AdminUserService
from app.services.pricing.admin_review_queue_service import AdminPricingReviewQueueService


router = APIRouter(prefix="/admin/reports", tags=["Admin Reports"])


@router.get("/overview")
def reports_overview(
    days: int = Query(30, ge=1, le=365),
    since: str | None = Query(default=None, min_length=1),
    until: str | None = Query(default=None, min_length=1),
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    payload = AdminReportsService().overview(days=days, since=since, until=until)
    _record_audit("admin_reports.overview_viewed", "success", {"summary": payload.get("summary", {})})
    return payload


@router.get("/export")
def reports_export(
    dataset: str = Query(pattern="^(users|pricing|scans|audit)$"),
    q: str | None = Query(default=None, min_length=1),
    reason: str | None = Query(default=None, min_length=1),
    action: str | None = Query(default=None, min_length=1),
    status_filter: str | None = Query(default=None, alias="status", min_length=1),
    target_type: str | None = Query(default=None, alias="targetType", min_length=1),
    target_id: str | None = Query(default=None, alias="targetId", min_length=1),
    actor: str | None = Query(default=None, min_length=1),
    since: str | None = Query(default=None, min_length=1),
    until: str | None = Query(default=None, min_length=1),
    limit: int = Query(200, ge=1, le=1000),
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> StreamingResponse:
    try:
        rows = _export_rows(
            dataset,
            q=q,
            reason=reason,
            action=action,
            status_filter=status_filter,
            target_type=target_type,
            target_id=target_id,
            actor=actor,
            since=since,
            until=until,
            limit=limit,
        )
    except Exception as error:
        _record_audit("admin_reports.exported", "failure", {"dataset": dataset, "error": str(error)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_export_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error
    _record_audit("admin_reports.exported", "success", {"dataset": dataset, "count": len(rows)})
    return StreamingResponse(
        iter([_csv_text(rows)]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="packlox-admin-{dataset}.csv"'},
    )


def _export_rows(
    dataset: str,
    *,
    q: str | None,
    reason: str | None,
    action: str | None,
    status_filter: str | None,
    target_type: str | None,
    target_id: str | None,
    actor: str | None,
    since: str | None,
    until: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if dataset == "users":
        return AdminUserService().list_users(query=q, limit=min(limit, 100)).get("users", [])
    if dataset == "pricing":
        return AdminPricingReviewQueueService().list_queue(
            reason=reason or "all",
            limit=min(limit, 200),
        ).get("items", [])
    if dataset == "scans":
        return AdminScanFailureService().list_failures(
            reason=reason or "all",
            limit=min(limit, 200),
        ).get("items", [])
    return AdminAuditService().list_events(
        action=action,
        status=status_filter,
        target_type=target_type,
        target_id=target_id,
        actor=actor,
        since=since,
        until=until,
        limit=min(limit, 1000),
    ).get("events", [])


def _csv_text(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    fieldnames = sorted({key for row in rows for key in row.keys()}) or ["empty"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})
    return output.getvalue()


def _csv_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return str(value)
    return "" if value is None else str(value)


def _record_audit(action: str, event_status: str, metadata: dict[str, Any]) -> None:
    try:
        AdminAuditService().record(action=action, status=event_status, metadata=metadata)
    except Exception:
        return
