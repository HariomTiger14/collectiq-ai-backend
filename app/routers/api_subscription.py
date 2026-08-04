from fastapi import APIRouter, Header, HTTPException, status

from app.schemas.api_subscription import (
    SubscriptionEntitlementResponse,
    VerifySubscriptionRequest,
)
from app.services.subscription.subscription_service import (
    SubscriptionNotConfiguredError,
    SubscriptionService,
    SubscriptionServiceError,
    SubscriptionUnauthorizedError,
)

router = APIRouter(prefix="/subscription", tags=["Subscription"])

_service = SubscriptionService()


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )
    return authorization.split(" ", 1)[1].strip()


def _handle(callable_):
    try:
        return callable_()
    except SubscriptionUnauthorizedError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session.",
        )
    except SubscriptionNotConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        )
    except SubscriptionServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        )


@router.get("/me", response_model=SubscriptionEntitlementResponse)
async def get_my_entitlement(
    authorization: str | None = Header(default=None),
) -> SubscriptionEntitlementResponse:
    token = _bearer_token(authorization)

    def _run() -> SubscriptionEntitlementResponse:
        user_id = _service.user_id_from_token(token)
        return SubscriptionEntitlementResponse(**_service.get_entitlement(user_id))

    return _handle(_run)


@router.post("/verify", response_model=SubscriptionEntitlementResponse)
async def verify_subscription(
    payload: VerifySubscriptionRequest,
    authorization: str | None = Header(default=None),
) -> SubscriptionEntitlementResponse:
    token = _bearer_token(authorization)

    def _run() -> SubscriptionEntitlementResponse:
        user_id = _service.user_id_from_token(token)
        entitlement = _service.verify_and_grant(
            user_id=user_id,
            plan=payload.plan,
            source=payload.source,
            purchase_token=payload.purchaseToken,
        )
        return SubscriptionEntitlementResponse(**entitlement)

    return _handle(_run)
