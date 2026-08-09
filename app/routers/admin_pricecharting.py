from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.config import settings
from app.routers.admin_auth import require_admin_import_token
from app.services.admin_import_job_service import AdminImportJobService
from scripts.import_pricecharting_catalog import PRICECHARTING_CSV_ENV_VARS
from scripts.import_pricecharting_catalog import (
    PartialCatalogWriteError,
    SupabaseCatalogClient,
    dedupe_catalog_rows,
    download_env_sources,
    to_catalog_row,
)


router = APIRouter(prefix="/admin/pricecharting", tags=["Admin"])


@router.post("/import")
def import_pricecharting_catalog(
    dry_run: bool = Query(True, alias="dryRun"),
    source: str | None = Query(
        default=None,
        description="Optional source key, for example video_games or pokemon.",
    ),
    timeout_seconds: int = Query(
        180,
        alias="timeoutSeconds",
        ge=10,
        le=600,
        description="Download/import timeout in seconds.",
    ),
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    source_filter = _normalized_source_filter(source)
    jobs = AdminImportJobService()
    job = jobs.create_job(source=source_filter or "all", dry_run=dry_run)
    try:
        sources = download_env_sources(
            timeout_seconds=timeout_seconds,
            source_filter=source_filter,
        )
    except SystemExit as exc:
        jobs.fail_job(job["id"], str(exc))
        raise _admin_import_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_import_source",
            str(exc),
        ) from exc
    except httpx.TimeoutException as exc:
        jobs.fail_job(job["id"], "Timed out while downloading the PriceCharting CSV.")
        raise _admin_import_error(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "pricecharting_csv_timeout",
            "Timed out while downloading the PriceCharting CSV.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        jobs.fail_job(job["id"], f"PriceCharting CSV download failed with HTTP {exc.response.status_code}.")
        raise _admin_import_error(
            status.HTTP_502_BAD_GATEWAY,
            "pricecharting_csv_download_failed",
            f"PriceCharting CSV download failed with HTTP {exc.response.status_code}.",
        ) from exc
    except httpx.HTTPError as exc:
        jobs.fail_job(job["id"], "PriceCharting CSV download failed.")
        raise _admin_import_error(
            status.HTTP_502_BAD_GATEWAY,
            "pricecharting_csv_download_failed",
            "PriceCharting CSV download failed.",
        ) from exc

    imported_rows: list[dict[str, Any]] = []
    source_summaries: list[dict[str, Any]] = []
    for source in sources:
        source_rows = [
            to_catalog_row(
                row,
                source.name,
                settings.build_time,
            )
            for row in source.rows
        ]
        source_rows = [row for row in source_rows if row is not None]
        imported_rows.extend(source_rows)
        source_summaries.append(
            {
                "source": source.name,
                "inputRows": len(source.rows),
                "validRows": len(source_rows),
            }
        )

    imported_rows = dedupe_catalog_rows(imported_rows)

    imported_count = 0
    history_count = 0
    if not dry_run:
        try:
            client = SupabaseCatalogClient(
                supabase_url=settings.supabase_url,
                service_role_key=settings.supabase_service_role_key,
                timeout_seconds=timeout_seconds,
            )
            history_count = client.sync_scd2_history_rows(imported_rows, batch_size=500)
            imported_count = client.upsert_rows(imported_rows, batch_size=500)
        except SystemExit as exc:
            jobs.fail_job(job["id"], str(exc))
            raise _admin_import_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "supabase_import_not_configured",
                str(exc),
            ) from exc
        except PartialCatalogWriteError as exc:
            # sync_scd2_history_rows()/upsert_rows() now attempt every
            # sub-batch before reporting failure (see PartialCatalogWriteError)
            # instead of raising SystemExit on the first failing sub-batch --
            # still a real failure for this endpoint's contract (some rows
            # didn't write), just with more of the work already done.
            jobs.fail_job(job["id"], str(exc))
            raise _admin_import_error(
                status.HTTP_502_BAD_GATEWAY,
                "supabase_import_failed",
                str(exc),
            ) from exc
        except httpx.HTTPError as exc:
            jobs.fail_job(job["id"], "Supabase catalog import failed.")
            raise _admin_import_error(
                status.HTTP_502_BAD_GATEWAY,
                "supabase_import_failed",
                "Supabase catalog import failed.",
            ) from exc

    payload = {
        "success": True,
        "jobId": job["id"],
        "dryRun": dry_run,
        "source": source_filter or "all",
        "sources": source_summaries,
        "inputRows": sum(source["inputRows"] for source in source_summaries),
        "validRows": len(imported_rows),
        "importedRows": imported_count,
        "historyRows": history_count,
    }
    jobs.complete_job(job["id"], payload)
    return payload


@router.get("/import-jobs")
def list_pricecharting_import_jobs(
    limit: int = Query(25, ge=1, le=100),
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    return AdminImportJobService().list_jobs(limit=limit)


def _normalized_source_filter(source: str | None) -> str | None:
    if source is None or not source.strip():
        return None
    normalized = source.strip().lower().replace("-", "_")
    if normalized not in PRICECHARTING_CSV_ENV_VARS:
        allowed_sources = ", ".join(sorted(PRICECHARTING_CSV_ENV_VARS))
        raise _admin_import_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "unsupported_import_source",
            f"Unsupported source. Use one of: {allowed_sources}.",
        )
    return normalized


def _admin_import_error(
    status_code: int,
    code: str,
    message: str,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": status_code >= 500,
        },
    )
