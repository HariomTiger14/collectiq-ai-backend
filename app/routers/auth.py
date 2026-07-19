from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services.auth.signup_start_guard import (
    SignupStartGuardError,
    SignupStartRateLimitedError,
    SignupStartThrottle,
    SupabaseSignupStartGuard,
)


router = APIRouter(prefix="/auth", tags=["Auth"])
_throttle = SignupStartThrottle()


class SignupStartRequest(BaseModel):
    email: str


class SignupStartResponse(BaseModel):
    safeForAccountCreation: bool
    delivery: str = "otpCode"
    cooldownSeconds: int = 30


def _client_key(request: Request, email: str) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    host = forwarded_for.split(",", 1)[0].strip() or (
        request.client.host if request.client else "unknown"
    )
    return f"{host}:{email.strip().lower()}"


@router.post("/signup-start", response_model=SignupStartResponse)
def signup_start(payload: SignupStartRequest, request: Request) -> SignupStartResponse:
    normalized_email = payload.email.strip().lower()
    if (
        "@" not in normalized_email
        or normalized_email.startswith("@")
        or normalized_email.endswith("@")
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_email",
                "message": "Valid email is required.",
                "retryable": False,
            },
        )
    try:
        _throttle.check(_client_key(request, normalized_email))
        decision = SupabaseSignupStartGuard().start(email=normalized_email)
    except SignupStartRateLimitedError:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "message": "Please wait before trying again.",
                "retryable": True,
            },
        )
    except SignupStartGuardError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "signup_start_unavailable",
                "message": "Signup start is temporarily unavailable.",
                "retryable": True,
            },
        )

    return SignupStartResponse(
        safeForAccountCreation=decision.safe_for_account_creation,
        cooldownSeconds=decision.cooldown_seconds,
    )
