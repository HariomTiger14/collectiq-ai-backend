from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.routers.admin_auth import require_admin_job_token
from app.services.data_requests.data_request_service import (
    DataRequestError,
    DataRequestNotFoundError,
    DataRequestService,
    DataRequestUnauthorizedError,
)

router = APIRouter(tags=["Data Requests"])

_service = DataRequestService()


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )
    return authorization.split(" ", 1)[1].strip()


# -- user-facing: a signed-in user filing/checking their own requests -------


@router.post("/data-requests")
async def create_data_request(
    type: str = Query(..., pattern="^(export|deletion)$"),
    authorization: str | None = Header(default=None),
) -> dict:
    token = _bearer_token(authorization)
    try:
        user_id = _service.user_id_from_token(token)
        return _service.create_request(user_id=user_id, request_type=type)
    except DataRequestUnauthorizedError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error
    except DataRequestError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/data-requests/mine")
async def list_my_data_requests(
    authorization: str | None = Header(default=None),
) -> dict:
    token = _bearer_token(authorization)
    try:
        user_id = _service.user_id_from_token(token)
        return {"success": True, "requests": _service.list_my_requests(user_id)}
    except DataRequestUnauthorizedError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error
    except DataRequestError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


# -- admin: the console's Data requests queue --------------------------------


@router.get("/admin/data-requests")
async def list_admin_data_requests(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    try:
        return _service.list_requests(status=status_filter, limit=limit)
    except DataRequestError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "data_requests_unavailable", "message": str(error), "retryable": True},
        ) from error


@router.post("/admin/data-requests/{request_id}/process")
async def process_admin_data_request(
    request_id: str,
    dry_run: bool = Query(True, alias="dryRun"),
    _admin: None = Depends(require_admin_job_token),
) -> dict:
    try:
        return _service.process_request(request_id, dry_run=dry_run)
    except DataRequestNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except DataRequestError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "data_request_process_failed", "message": str(error), "retryable": True},
        ) from error
