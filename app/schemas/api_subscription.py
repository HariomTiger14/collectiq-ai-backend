from pydantic import BaseModel, Field


class VerifySubscriptionRequest(BaseModel):
    """A client-reported purchase to record as an entitlement.

    In mock mode the purchaseToken is trusted; when real store validation is
    added it will be verified against Google/Apple before the plan is granted.
    """

    plan: str = Field(default="pro")
    source: str = Field(default="mock")
    purchaseToken: str | None = Field(default=None)


class SubscriptionEntitlementResponse(BaseModel):
    plan: str
    status: str
    source: str
    currentPeriodEnd: str | None = Field(default=None)
