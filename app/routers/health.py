"""Health check routes."""

from fastapi import APIRouter

from app.core.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
def get_health() -> dict[str, str]:
    """Return service health and version information."""
    return {"status": "ok", "version": settings.version}
