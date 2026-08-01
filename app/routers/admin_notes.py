from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.routers.admin_auth import require_admin_import_token, require_admin_permission
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_notes_service import AdminNotesError, AdminNotesService


router = APIRouter(prefix="/admin/notes", tags=["Admin Notes"])


class AdminNoteRequest(BaseModel):
    targetType: str = Field(pattern="^(user|catalog_item|portfolio_item|scan_analysis|pricing|audit_event)$")
    targetId: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=2000)
    metadata: dict[str, Any] | None = None


@router.get("")
def list_admin_notes(
    target_type: str = Query(alias="targetType", pattern="^(user|catalog_item|portfolio_item|scan_analysis|pricing|audit_event)$"),
    target_id: str = Query(alias="targetId", min_length=1),
    limit: int = Query(50, ge=1, le=100),
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return AdminNotesService().list_notes(
            target_type=target_type,
            target_id=target_id,
            limit=limit,
        )
    except AdminNotesError as error:
        raise _notes_error(str(error)) from error


@router.post("")
def create_admin_note(
    request: AdminNoteRequest,
    admin: dict[str, Any] = Depends(require_admin_permission("admin:read")),
) -> dict[str, Any]:
    actor = str(admin.get("email") or admin.get("id") or "admin")
    try:
        payload = AdminNotesService().create_note(
            target_type=request.targetType,
            target_id=request.targetId,
            note=request.note,
            actor=actor,
            metadata=request.metadata,
        )
        _record_audit(
            "admin_notes.created",
            "success",
            request.targetType,
            request.targetId,
            {"noteId": payload.get("note", {}).get("id")},
        )
        return payload
    except AdminNotesError as error:
        _record_audit(
            "admin_notes.created",
            "failure",
            request.targetType,
            request.targetId,
            {"error": str(error)},
        )
        raise _notes_error(str(error)) from error


def _notes_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "admin_notes_unavailable",
            "message": message,
            "retryable": True,
        },
    )


def _record_audit(
    action: str,
    event_status: str,
    target_type: str,
    target_id: str,
    metadata: dict[str, Any],
) -> None:
    try:
        AdminAuditService().record(
            action=action,
            status=event_status,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata,
        )
    except Exception:
        return
