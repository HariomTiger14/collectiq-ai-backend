from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.routers.admin_auth import require_admin_import_token
from app.services.pricing.admin_health_service import PricingHealthService
from app.services.pricing.admin_health_service import PricingHealthError


router = APIRouter(prefix="/admin/pricing", tags=["Admin Pricing"])


@router.get("/health")
def pricing_health(
    _admin: None = Depends(require_admin_import_token),
) -> dict[str, Any]:
    try:
        return PricingHealthService().health()
    except PricingHealthError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "pricing_health_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error
