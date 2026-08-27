from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from app.services.ops.observability import recorded_admin_job

from app.routers.admin_auth import require_admin_job_token
from app.services.pricing.portfolio_catalog_matching_service import (
    PortfolioCatalogMatchingError,
    match_unlinked_portfolio_items,
)


router = APIRouter(prefix="/admin/portfolio", tags=["Admin"])


@router.post("/match-catalog")
@recorded_admin_job("match-portfolio-catalog")
def match_portfolio_items_to_catalog(
    dry_run: bool = Query(True, alias="dryRun"),
    limit: int = Query(200, ge=1, le=2000),
    timeout_seconds: int = Query(30, alias="timeoutSeconds", ge=5, le=120),
    _admin: dict[str, Any] = Depends(require_admin_job_token),
) -> dict[str, Any]:
    try:
        result = match_unlinked_portfolio_items(
            limit=limit,
            dry_run=dry_run,
            timeout_seconds=timeout_seconds,
        )
    except PortfolioCatalogMatchingError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "portfolio_catalog_matching_failed",
                "message": str(exc),
                "retryable": True,
            },
        ) from exc

    return {
        "success": True,
        "dryRun": dry_run,
        "candidateCount": result.candidateCount,
        "matchedCount": result.matchedCount,
        "unmatchedCount": result.unmatchedCount,
        "skippedMissingTitle": result.skippedMissingTitle,
        "updateFailures": result.updateFailures,
        "historyRowsBackfilled": result.historyRowsBackfilled,
    }
