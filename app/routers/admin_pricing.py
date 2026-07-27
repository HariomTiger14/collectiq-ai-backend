from typing import Any

from fastapi import APIRouter, Header, HTTPException, status

from app.routers.admin_pricecharting import _require_admin_token
from app.services.pricing.admin_health_service import PricingHealthService
from app.services.pricing.admin_health_service import PricingHealthError


router = APIRouter(prefix="/admin/pricing", tags=["Admin Pricing"])


@router.get("/health")
def pricing_health(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin_token(
        x_admin_token=x_admin_token,
        authorization=authorization,
    )
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
