from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from app.core.config import settings


class SignupStartGuardError(Exception):
    """Raised when the signup-start guard cannot make a safe decision."""


class SignupStartRateLimitedError(Exception):
    """Raised when signup-start requests exceed the local throttle."""


@dataclass(frozen=True)
class SignupStartGuardDecision:
    safe_for_account_creation: bool
    cooldown_seconds: int = 30


class SignupStartThrottle:
    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: int = 300,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def check(self, key: str, now: float | None = None) -> None:
        current_time = now if now is not None else time.monotonic()
        window_start = current_time - self.window_seconds
        attempts = [
            timestamp
            for timestamp in self._attempts.get(key, [])
            if timestamp >= window_start
        ]
        if len(attempts) >= self.max_attempts:
            self._attempts[key] = attempts
            raise SignupStartRateLimitedError()
        attempts.append(current_time)
        self._attempts[key] = attempts


class SupabaseSignupStartGuard:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        timeout_seconds: float = 5,
        client: httpx.Client | None = None,
    ) -> None:
        self._supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).strip().rstrip("/")
        self._service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        ).strip()
        self._timeout_seconds = timeout_seconds
        self._client = client

    def start(self, *, email: str) -> SignupStartGuardDecision:
        normalized_email = email.strip().lower()
        if not normalized_email:
            raise SignupStartGuardError("Email is required.")
        if not self._supabase_url or not self._service_role_key:
            raise SignupStartGuardError("Supabase admin configuration is missing.")

        users = self._matching_users(normalized_email)
        return SignupStartGuardDecision(
            safe_for_account_creation=not any(
                self._is_existing_account(user, normalized_email) for user in users
            )
        )

    def _matching_users(self, email: str) -> list[dict]:
        url = f"{self._supabase_url}/auth/v1/admin/users"
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
        }
        params = {"email": email}
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise SignupStartGuardError("Supabase admin lookup failed.") from error
        finally:
            if should_close:
                client.close()

        if isinstance(payload, dict):
            raw_users = payload.get("users", [])
        elif isinstance(payload, list):
            raw_users = payload
        else:
            raise SignupStartGuardError("Supabase admin lookup returned invalid data.")
        if not isinstance(raw_users, list):
            raise SignupStartGuardError("Supabase admin lookup returned invalid data.")
        return [user for user in raw_users if isinstance(user, dict)]

    def _is_existing_account(self, user: dict, email: str) -> bool:
        user_email = str(user.get("email") or "").strip().lower()
        if user_email != email:
            return False
        if user.get("deleted_at"):
            return False
        return bool(user.get("email_confirmed_at") or user.get("confirmed_at"))
