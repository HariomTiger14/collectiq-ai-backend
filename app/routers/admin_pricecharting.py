from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, status

from app.core.config import settings
from scripts.import_pricecharting_catalog import PRICECHARTING_CSV_ENV_VARS
from scripts.import_pricecharting_catalog import (
    SupabaseCatalogClient,
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
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin_token(
        x_admin_token=x_admin_token,
        authorization=authorization,
    )

    source_filter = _normalized_source_filter(source)
    try:
        sources = download_env_sources(
            timeout_seconds=30,
            source_filter=source_filter,
        )
    except SystemExit as exc:
        raise _admin_import_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_import_source",
            str(exc),
        ) from exc
    except httpx.TimeoutException as exc:
        raise _admin_import_error(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "pricecharting_csv_timeout",
            "Timed out while downloading the PriceCharting CSV.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise _admin_import_error(
            status.HTTP_502_BAD_GATEWAY,
            "pricecharting_csv_download_failed",
            f"PriceCharting CSV download failed with HTTP {exc.response.status_code}.",
        ) from exc
    except httpx.HTTPError as exc:
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

    imported_count = 0
    if not dry_run:
        try:
            client = SupabaseCatalogClient(
                supabase_url=settings.supabase_url,
                service_role_key=settings.supabase_service_role_key,
                timeout_seconds=30,
            )
            imported_count = client.upsert_rows(imported_rows, batch_size=500)
        except SystemExit as exc:
            raise _admin_import_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "supabase_import_not_configured",
                str(exc),
            ) from exc
        except httpx.HTTPError as exc:
            raise _admin_import_error(
                status.HTTP_502_BAD_GATEWAY,
                "supabase_import_failed",
                "Supabase catalog import failed.",
            ) from exc

    return {
        "success": True,
        "dryRun": dry_run,
        "source": source_filter or "all",
        "sources": source_summaries,
        "inputRows": sum(source["inputRows"] for source in source_summaries),
        "validRows": len(imported_rows),
        "importedRows": imported_count,
    }


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


def _require_admin_token(
    *,
    x_admin_token: str | None,
    authorization: str | None,
) -> None:
    expected_token = settings.admin_import_token.strip()
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "admin_import_not_configured",
                "message": "ADMIN_IMPORT_TOKEN is not configured.",
                "retryable": False,
            },
        )

    bearer_token = ""
    if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        bearer_token = authorization.split(" ", 1)[1].strip()
    supplied_token = (x_admin_token or bearer_token or "").strip()
    if supplied_token != expected_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "unauthorized",
                "message": "Admin token is invalid.",
                "retryable": False,
            },
        )
