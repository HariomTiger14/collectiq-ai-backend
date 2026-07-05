from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.services.health.health_check_service import HealthCheckService


router = APIRouter(tags=["System"])


@router.get("/health")
async def health() -> JSONResponse:
    report = HealthCheckService().run()
    status_code = 200 if report.healthy else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "healthy" if report.healthy else "unhealthy",
            "environment": settings.environment,
            "version": settings.version,
            "timestamp": report.timestamp,
            "services": report.services,
            "latency": report.latency,
            "checks": [
                {
                    "name": check.name,
                    "healthy": check.healthy,
                    "required": check.required,
                    "latencyMs": check.latency_ms,
                    "message": check.message,
                    "details": check.details,
                }
                for check in report.checks
            ],
        },
    )


@router.get("/version")
async def version() -> dict[str, str]:
    return {
        "application": settings.application_name,
        "environment": settings.environment,
        "version": settings.version,
        "commit": settings.commit,
        "buildTime": _build_time(),
    }


def _build_time() -> str:
    if settings.build_time != "unknown":
        return settings.build_time
    return datetime.now(UTC).isoformat()
