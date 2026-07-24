from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, status

from app.core.config import settings
from scripts.import_pricecharting_catalog import (
    SupabaseCatalogClient,
    download_env_sources,
    to_catalog_row,
)


router = APIRouter(prefix="/admin/pricecharting", tags=["Admin"])


@router.post("/import")
def import_pricecharting_catalog(
    dry_run: bool = Query(True, alias="dryRun"),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin_token(
        x_admin_token=x_admin_token,
        authorization=authorization,
    )

    sources = download_env_sources(timeout_seconds=30)
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
        client = SupabaseCatalogClient(
            supabase_url=settings.supabase_url,
            service_role_key=settings.supabase_service_role_key,
            timeout_seconds=30,
        )
        imported_count = client.upsert_rows(imported_rows, batch_size=500)

    return {
        "success": True,
        "dryRun": dry_run,
        "sources": source_summaries,
        "inputRows": sum(source["inputRows"] for source in source_summaries),
        "validRows": len(imported_rows),
        "importedRows": imported_count,
    }


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
