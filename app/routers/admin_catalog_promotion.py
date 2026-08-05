from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.routers.admin_auth import require_admin_import_token
from app.services.admin_import_job_service import AdminImportJobService
from app.services.pricing.promote_scan_derived_catalog import (
    ScanDerivedPromotionError,
    promote_scan_derived_rows,
)


router = APIRouter(prefix="/admin/catalog", tags=["Admin"])


@router.post("/promote-scan-derived")
def promote_scan_derived_catalog_rows(
    dry_run: bool = Query(True, alias="dryRun"),
    min_hit_count: int = Query(
        1,
        alias="minHitCount",
        ge=0,
        le=1000,
        description=(
            "Minimum pricing_cache_entries.hit_count to qualify. hit_count "
            "starts at 0 and only increments on a repeat scan matching the "
            "same normalized identity, so 1 already means at least one "
            "independent corroborating scan beyond the original."
        ),
    ),
    limit: int = Query(500, ge=1, le=2000),
    timeout_seconds: int = Query(30, alias="timeoutSeconds", ge=5, le=120),
    _admin: dict[str, Any] = Depends(require_admin_import_token),
) -> dict[str, Any]:
    jobs = AdminImportJobService()
    job = jobs.create_job(source="scan_derived", dry_run=dry_run)
    try:
        result = promote_scan_derived_rows(
            min_hit_count=min_hit_count,
            dry_run=dry_run,
            limit=limit,
            timeout_seconds=timeout_seconds,
        )
    except ScanDerivedPromotionError as exc:
        jobs.fail_job(job["id"], str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "scan_derived_promotion_failed",
                "message": str(exc),
                "retryable": True,
            },
        ) from exc

    payload = {
        "success": True,
        "jobId": job["id"],
        "dryRun": dry_run,
        "minHitCount": min_hit_count,
        "candidateCount": result.candidateCount,
        "promotedRows": result.promotedCount,
        "skippedAlreadyPromoted": result.skippedAlreadyPromoted,
        "skippedMissingTitle": result.skippedMissingTitle,
        "historyRows": result.historyRows,
    }
    jobs.complete_job(
        job["id"],
        {
            "inputRows": result.candidateCount,
            "validRows": (
                result.candidateCount
                - result.skippedAlreadyPromoted
                - result.skippedMissingTitle
            ),
            "importedRows": result.promotedCount,
            "historyRows": result.historyRows,
        },
    )
    return payload


# Job history for these runs is visible via the existing
# GET /admin/pricecharting/import-jobs endpoint — AdminImportJobService's
# job list isn't source-scoped, and these jobs are distinguishable there by
# source="scan_derived". No need for a duplicate listing endpoint here.
