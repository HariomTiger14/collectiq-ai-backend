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
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    payload = AdminReportsService().overview()
    _record_audit("admin_reports.overview_viewed", "success", {"summary": payload.get("summary", {})})
    return payload


@router.get("/export")
def reports_export(
    dataset: str = Query(pattern="^(users|pricing|scans|audit)$"),
    _admin: None = Depends(require_admin_import_token),
) -> StreamingResponse:
    try:
        rows = _export_rows(dataset)
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


def _export_rows(dataset: str) -> list[dict[str, Any]]:
    if dataset == "users":
        return AdminUserService().list_users(limit=100).get("users", [])
    if dataset == "pricing":
        return AdminPricingReviewQueueService().list_queue(limit=200).get("items", [])
    if dataset == "scans":
        return AdminScanFailureService().list_failures(limit=200).get("items", [])
    return AdminAuditService().list_events(limit=200).get("events", [])


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
