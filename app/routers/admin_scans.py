from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.routers.admin_auth import require_admin_import_token
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_scan_failure_service import (
    AdminScanFailureService,
    ScanFailureNotFoundError,
    ScanFailureQueueError,
)


router = APIRouter(prefix="/admin/scans", tags=["Admin Scans"])


class ScanFailureResolveRequest(BaseModel):
    category: str = Field(pattern="^(provider_error|low_confidence|image_quality|unpriced|duplicate_invalid|needs_review)$")
    note: str | None = Field(default=None, max_length=1000)


@router.get("/failures")
def scan_failures(
    reason: str = Query(
        "all",
        pattern="^(all|provider_error|low_confidence|image_quality|unpriced|needs_review)$",
    ),
    limit: int = Query(50, ge=1, le=200),
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminScanFailureService().list_failures(reason=reason, limit=limit)
        _record_audit(
            action="scan_failure_queue.viewed",
            status="success",
            metadata={"filter": reason, "count": payload.get("count", 0)},
        )
        return payload
    except ScanFailureQueueError as error:
        _record_audit(
            action="scan_failure_queue.viewed",
            status="failure",
            metadata={"filter": reason, "error": str(error)},
        )
        raise _scan_queue_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scan_failure_queue_unavailable",
            str(error),
            retryable=True,
        ) from error


@router.post("/failures/{scan_id}/reviewed")
def mark_scan_failure_reviewed(
    scan_id: str,
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminScanFailureService().mark_reviewed(scan_id)
        _record_audit(
            action="scan_failure_queue.mark_reviewed",
            status="success",
            target_id=scan_id,
        )
        return payload
    except ScanFailureNotFoundError as error:
        _record_audit(
            action="scan_failure_queue.mark_reviewed",
            status="failure",
            target_id=scan_id,
            metadata={"error": str(error)},
        )
        raise _scan_queue_error(
            status.HTTP_404_NOT_FOUND,
            "scan_failure_not_found",
            str(error),
        ) from error
    except ScanFailureQueueError as error:
        _record_audit(
            action="scan_failure_queue.mark_reviewed",
            status="failure",
            target_id=scan_id,
            metadata={"error": str(error)},
        )
        raise _scan_queue_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scan_failure_queue_unavailable",
            str(error),
            retryable=True,
        ) from error


@router.post("/failures/{scan_id}/resolved")
def resolve_scan_failure(
    scan_id: str,
    request: ScanFailureResolveRequest,
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminScanFailureService().resolve_failure(
            scan_id,
            category=request.category,
            note=request.note,
        )
        _record_audit(
            action="scan_failure_queue.resolve",
            status="success",
            target_id=scan_id,
            metadata={"category": request.category, "hasNote": bool(request.note)},
        )
        return payload
    except ScanFailureNotFoundError as error:
        _record_audit(
            action="scan_failure_queue.resolve",
            status="failure",
            target_id=scan_id,
            metadata={"error": str(error)},
        )
        raise _scan_queue_error(
            status.HTTP_404_NOT_FOUND,
            "scan_failure_not_found",
            str(error),
        ) from error
    except ScanFailureQueueError as error:
        _record_audit(
            action="scan_failure_queue.resolve",
            status="failure",
            target_id=scan_id,
            metadata={"error": str(error)},
        )
        raise _scan_queue_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scan_failure_queue_unavailable",
            str(error),
            retryable=True,
        ) from error


@router.post("/failures/{scan_id}/retry")
def retry_scan_failure_analysis(
    scan_id: str,
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        payload = AdminScanFailureService().retry_analysis(scan_id)
        _record_audit(
            action="scan_failure_queue.retry_analysis",
            status="success",
            target_id=scan_id,
        )
        return payload
    except ScanFailureNotFoundError as error:
        _record_audit(
            action="scan_failure_queue.retry_analysis",
            status="failure",
            target_id=scan_id,
            metadata={"error": str(error)},
        )
        raise _scan_queue_error(
            status.HTTP_404_NOT_FOUND,
            "scan_failure_not_found",
            str(error),
        ) from error
    except ScanFailureQueueError as error:
        _record_audit(
            action="scan_failure_queue.retry_analysis",
            status="failure",
            target_id=scan_id,
            metadata={"error": str(error)},
        )
        raise _scan_queue_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scan_failure_queue_unavailable",
            str(error),
            retryable=True,
        ) from error


def _scan_queue_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": retryable},
    )


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
            target_type="scan_analysis" if target_id else None,
            target_id=target_id,
            metadata=metadata,
        )
    except Exception:
        return
