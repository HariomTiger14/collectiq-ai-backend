from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.config import settings
from app.routers.admin_auth import require_admin_import_token
from app.services.ops.pipeline_health_service import (
    AdminOpsObservabilityError,
    AdminOpsObservabilityService,
)
from app.services.health.health_check_service import HealthCheckService
from app.services.pricing.admin_health_service import PricingHealthError
from app.services.pricing.admin_health_service import PricingHealthService
from scripts.import_pricecharting_catalog import PRICECHARTING_CSV_ENV_VARS


router = APIRouter(prefix="/admin/ops", tags=["Admin Operations"])
BACKEND_ROOT = Path(__file__).resolve().parents[2]


@router.get("/readiness")
def ops_readiness(
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    # A fast subset of /admin/ops/summary — just the config-readiness
    # checklist, which is pure in-memory settings inspection (sub-ms).
    # The full summary also runs PricingHealthService().health(), which
    # calls a Supabase RPC (pricecharting_catalog_health_summary) that has
    # been observed taking ~49s in production against the real catalog
    # tables (known unindexed-aggregation issue, tracked separately — see
    # A-to-Z doc §7). Callers that only need readiness (e.g. the admin
    # console's Dashboard, which never reads summary.pricing or
    # summary.health) should use this endpoint instead of paying that cost.
    return {
        "success": True,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "readiness": _configuration_readiness(),
    }


@router.get("/pipeline-health")
def ops_pipeline_health(
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    # The Scheduled-jobs board: per-job status merging the run ledger
    # (ops_cron_runs), data-freshness probes (admin_pipeline_health RPC),
    # and each job's cadence. See AdminOpsObservabilityService for the
    # status semantics -- notably 'stale', which fires on dead OUTPUT even
    # when runs report success (the KicksDB-quota class of silent failure).
    try:
        return AdminOpsObservabilityService().pipeline_health()
    except AdminOpsObservabilityError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "ops_pipeline_health_unavailable", "message": str(error), "retryable": True},
        ) from error


@router.get("/runs")
def ops_runs(
    job: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return AdminOpsObservabilityService().runs(job=job, limit=limit)
    except AdminOpsObservabilityError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "ops_runs_unavailable", "message": str(error), "retryable": True},
        ) from error


@router.get("/errors")
def ops_errors(
    fingerprint: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    # Without a fingerprint: grouped error feed (one row per distinct
    # route/job + error class). With one: the individual occurrences,
    # stacks included.
    try:
        return AdminOpsObservabilityService().errors(fingerprint=fingerprint, limit=limit)
    except AdminOpsObservabilityError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "ops_errors_unavailable", "message": str(error), "retryable": True},
        ) from error


@router.get("/summary")
def ops_summary(
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    health_report = HealthCheckService().run()
    pricing = _pricing_health()
    validation = _validation_status()
    readiness = _configuration_readiness()

    status = _overall_status(
        api_healthy=health_report.healthy,
        pricing_status=str(pricing.get("status") or "unknown"),
        readiness=readiness,
    )

    return {
        "success": True,
        "status": status,
        "generatedAt": generated_at,
        "environment": {
            "name": settings.environment,
            "application": settings.application_name,
            "version": settings.version,
            "commit": settings.commit,
            "buildTime": settings.build_time,
            "publicApiUrl": settings.public_api_url,
            "publicFrontendUrl": settings.public_frontend_url,
        },
        "health": {
            "status": "healthy" if health_report.healthy else "unhealthy",
            "services": health_report.services,
            "latency": health_report.latency,
            "checks": [
                {
                    "name": check.name,
                    "healthy": check.healthy,
                    "required": check.required,
                    "latencyMs": check.latency_ms,
                    "message": check.message,
                }
                for check in health_report.checks
            ],
        },
        "pricing": pricing,
        "validation": validation,
        "readiness": readiness,
        "maintenance": {
            "databaseMigrations": {
                "latestAdminPortalMigration": "database/migrations/20260801_admin_portal_operations.sql",
                "expectedTables": [
                    "admin_import_jobs",
                    "admin_notes",
                    "admin_audit_events",
                ],
                "expectedCatalogColumns": ["admin_note", "active"],
                "status": "apply_in_supabase_before_live_use",
            },
            "catalogImport": {
                "available": True,
                "sources": _pricecharting_sources(),
                "supportsDryRun": True,
                "supportsHistorySync": True,
            },
            "providerThrottle": {
                "sharedPriceChartingThrottleEnabled": settings.pricecharting_shared_throttle_enabled,
                "pricechartingMinIntervalMs": settings.pricecharting_provider_min_interval_ms,
                "defaultProviderMinIntervalMs": settings.pricing_provider_min_interval_ms,
                "cacheTtlSeconds": settings.pricing_cache_ttl_seconds,
            },
            "auditLogs": {
                "available": True,
                "status": "exposed",
                "message": "Audit events are available through /admin/audit/events.",
            },
        },
    }


def _pricing_health() -> dict[str, Any]:
    try:
        payload = PricingHealthService().health()
    except PricingHealthError as error:
        return {
            "success": False,
            "status": "unavailable",
            "error": {
                "code": "pricing_health_unavailable",
                "message": str(error),
            },
        }
    return payload


def _validation_status() -> dict[str, Any]:
    reports_dir = BACKEND_ROOT / "validation" / "reports"
    markdown_report = reports_dir / "latest_validation_report.md"
    csv_report = reports_dir / "latest_validation_results.csv"
    manifest_dir = BACKEND_ROOT / "validation" / "manifests"
    latest_manifest = manifest_dir / "generated_manifest.json"

    report_files = [
        _file_status("markdownReport", markdown_report),
        _file_status("csvResults", csv_report),
        _file_status("generatedManifest", latest_manifest),
    ]
    has_report = any(file["exists"] for file in report_files[:2])
    return {
        "status": "ready" if has_report else "not_run",
        "reportsDirectory": str(reports_dir),
        "latestReportAvailable": has_report,
        "files": report_files,
        "workflows": {
            "datasetImporter": "available",
            "realProviderValidation": "manual",
            "reportEndpoint": "this_summary",
        },
    }


def _file_status(label: str, path: Path) -> dict[str, Any]:
    exists = path.exists()
    stat = path.stat() if exists else None
    return {
        "label": label,
        "path": str(path),
        "exists": exists,
        "sizeBytes": stat.st_size if stat else 0,
        "modifiedAt": datetime.fromtimestamp(
            stat.st_mtime,
            tz=timezone.utc,
        ).isoformat()
        if stat
        else None,
    }


def _configuration_readiness() -> list[dict[str, Any]]:
    return [
        _readiness("admin_import_token", "Admin token", bool(settings.admin_import_token.strip()), True),
        _readiness("admin_job_token", "Admin job token", bool(settings.admin_job_token.strip()), False),
        _readiness("supabase_url", "Supabase URL", bool(settings.supabase_url.strip()), True),
        _readiness(
            "supabase_service_role_key",
            "Supabase service role key",
            bool(settings.supabase_service_role_key.strip()),
            True,
        ),
        _readiness(
            "supabase_anon_key",
            "Supabase anon key",
            bool(settings.supabase_anon_key.strip()),
            False,
        ),
        _readiness("ai_provider", f"AI provider: {settings.ai_provider}", True, True),
        _readiness(
            "gemini_api_key",
            "Gemini key",
            bool(settings.gemini_api_key.strip()),
            settings.ai_provider in {"gemini", "auto"},
        ),
        _readiness(
            "openai_api_key",
            "OpenAI key",
            bool(settings.openai_api_key.strip()),
            settings.ai_provider in {"openai", "auto"},
        ),
        _readiness(
            "pricing_provider",
            f"Pricing provider: {settings.pricing_provider}",
            True,
            True,
        ),
        _readiness(
            "pricecharting_csv_sources",
            "PriceCharting CSV sources",
            any(source["configured"] for source in _pricecharting_sources()),
            True,
        ),
        _readiness(
            "firebase_push",
            "Firebase push credentials",
            bool(settings.firebase_project_id.strip())
            and bool(
                settings.firebase_service_account_json.strip()
                or settings.firebase_access_token.strip()
            ),
            False,
        ),
    ]


def _readiness(
    key: str,
    label: str,
    configured: bool,
    required: bool,
) -> dict[str, Any]:
    if configured:
        status = "configured"
    elif required:
        status = "missing"
    else:
        status = "optional_missing"
    return {
        "key": key,
        "label": label,
        "configured": configured,
        "required": required,
        "status": status,
    }


def _pricecharting_sources() -> list[dict[str, Any]]:
    return [
        {
            "key": source,
            "envVar": env_var,
            "configured": bool(os.getenv(env_var, "").strip()),
        }
        for source, env_var in sorted(PRICECHARTING_CSV_ENV_VARS.items())
    ]


def _overall_status(
    *,
    api_healthy: bool,
    pricing_status: str,
    readiness: list[dict[str, Any]],
) -> str:
    missing_required = [
        item for item in readiness if item["required"] and not item["configured"]
    ]
    if not api_healthy:
        return "unhealthy"
    if pricing_status in {"unhealthy", "unavailable"}:
        return "unhealthy"
    if pricing_status == "degraded" or missing_required:
        return "degraded"
    return "healthy"
