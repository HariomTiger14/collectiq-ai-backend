from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from pydantic import BaseModel

from app.routers.admin_auth import require_admin_job_token
from app.services.support.support_ticket_service import (
    MAX_ATTACHMENT_BYTES,
    SupportTicketError,
    SupportTicketNotFoundError,
    SupportTicketService,
    SupportTicketUnauthorizedError,
)

router = APIRouter(tags=["Support Tickets"])

_service = SupportTicketService()


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )
    return authorization.split(" ", 1)[1].strip()


def _handle(callable_):
    try:
        return callable_()
    except SupportTicketUnauthorizedError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error
    except SupportTicketNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except SupportTicketError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "support_ticket_unavailable", "message": str(error), "retryable": True},
        ) from error


class CreateTicketRequest(BaseModel):
    category: str
    subject: str
    message: str
    referencedItemId: str | None = None


class ReplyRequest(BaseModel):
    body: str


class SetStatusRequest(BaseModel):
    status: str


# -- user-facing ---------------------------------------------------------


@router.post("/support/tickets")
async def create_ticket(
    payload: CreateTicketRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    token = _bearer_token(authorization)

    def _run():
        user_id = _service.user_id_from_token(token)
        return _service.create_ticket(
            user_id=user_id,
            category=payload.category,
            subject=payload.subject,
            message=payload.message,
            referenced_item_id=payload.referencedItemId,
        )

    return _handle(_run)


@router.get("/support/tickets/mine")
async def list_my_tickets(authorization: str | None = Header(default=None)) -> dict:
    token = _bearer_token(authorization)

    def _run():
        user_id = _service.user_id_from_token(token)
        return {"success": True, "tickets": _service.list_my_tickets(user_id)}

    return _handle(_run)


@router.get("/support/tickets/{ticket_id}")
async def get_my_ticket_thread(
    ticket_id: str,
    authorization: str | None = Header(default=None),
) -> dict:
    token = _bearer_token(authorization)

    def _run():
        user_id = _service.user_id_from_token(token)
        return _service.get_ticket_thread(ticket_id=ticket_id, user_id=user_id)

    return _handle(_run)


@router.post("/support/tickets/{ticket_id}/reply")
async def reply_to_my_ticket(
    ticket_id: str,
    payload: ReplyRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    token = _bearer_token(authorization)

    def _run():
        user_id = _service.user_id_from_token(token)
        return _service.reply_as_user(user_id=user_id, ticket_id=ticket_id, body=payload.body)

    return _handle(_run)


@router.post("/support/messages/{message_id}/attachments")
async def upload_my_attachment(
    message_id: str,
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
) -> dict:
    token = _bearer_token(authorization)
    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large.")

    def _run():
        user_id = _service.user_id_from_token(token)
        return _service.add_attachment(
            message_id=message_id,
            file_name=file.filename or "attachment",
            content_type=file.content_type or "application/octet-stream",
            raw_bytes=raw_bytes,
            user_id=user_id,
        )

    return _handle(_run)


# -- admin -----------------------------------------------------------------


@router.get("/admin/support/tickets")
async def list_admin_tickets(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    return _handle(lambda: _service.list_tickets(status=status_filter, limit=limit))


@router.get("/admin/support/tickets/{ticket_id}")
async def get_admin_ticket_thread(
    ticket_id: str,
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    return _handle(lambda: _service.get_ticket_thread(ticket_id=ticket_id, user_id=None))


@router.post("/admin/support/tickets/{ticket_id}/reply")
async def reply_to_ticket_as_admin(
    ticket_id: str,
    payload: ReplyRequest,
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    return _handle(lambda: _service.reply_as_admin(ticket_id=ticket_id, body=payload.body))


@router.post("/admin/support/tickets/{ticket_id}/status")
async def set_admin_ticket_status(
    ticket_id: str,
    payload: SetStatusRequest,
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    return _handle(lambda: _service.set_ticket_status(ticket_id=ticket_id, status=payload.status))


@router.post("/admin/support/messages/{message_id}/attachments")
async def upload_admin_attachment(
    message_id: str,
    file: UploadFile = File(...),
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large.")
    return _handle(
        lambda: _service.add_attachment(
            message_id=message_id,
            file_name=file.filename or "attachment",
            content_type=file.content_type or "application/octet-stream",
            raw_bytes=raw_bytes,
            user_id=None,
        )
    )
