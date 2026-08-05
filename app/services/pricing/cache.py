import time
from dataclasses import dataclass
from threading import Lock

import httpx

from app.core.config import settings
from app.services.pricing.base_pricing_provider import PricingProviderRateLimitError


@dataclass(frozen=True)
class CacheEntry:
    value: object
    expires_at: float


class InMemoryPricingCache:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = max(0, ttl_seconds)
        self._items: dict[str, CacheEntry] = {}
        self._lock = Lock()

    def get(self, key: str):
        if self._ttl_seconds <= 0:
            return None

        now = time.time()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._items.pop(key, None)
                return None
            return entry.value

    def set(self, key: str, value) -> None:
        if self._ttl_seconds <= 0:
            return

        with self._lock:
            self._items[key] = CacheEntry(
                value=value,
                expires_at=time.time() + self._ttl_seconds,
            )

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class ProviderThrottle:
    def __init__(self, min_interval_ms: int) -> None:
        self._min_interval_seconds = max(0, min_interval_ms) / 1000
        self._last_request_at = 0.0
        self._lock = Lock()

    def acquire(self, provider_name: str) -> None:
        if self._min_interval_seconds <= 0:
            return

        now = time.monotonic()
        with self._lock:
            elapsed = now - self._last_request_at
            if elapsed < self._min_interval_seconds:
                retry_after = self._min_interval_seconds - elapsed
                raise PricingProviderRateLimitError(
                    f"{provider_name} throttled locally; retry after {retry_after:.2f}s."
                )
            self._last_request_at = now

    def reset(self) -> None:
        with self._lock:
            self._last_request_at = 0.0


_shared_client = httpx.Client(timeout=5)


class SharedProviderThrottle:
    """Database-backed provider gate shared by every backend worker."""

    def __init__(
        self,
        min_interval_ms: int,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        timeout_seconds: float = 5,
        client: httpx.Client | None = None,
    ) -> None:
        self._min_interval_ms = max(0, min_interval_ms)
        self._supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).strip().rstrip("/")
        self._service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        ).strip()
        self._timeout_seconds = timeout_seconds
        # Shared across requests — see shared_cache_repository.py for why this
        # isn't built fresh per instance.
        self._client = client or _shared_client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def acquire(self, provider_name: str) -> None:
        if self._min_interval_ms <= 0 or not self.is_configured:
            return

        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.post(
                f"{self._supabase_url}/rest/v1/rpc/acquire_pricing_provider_throttle",
                headers=headers,
                json={
                    "provider_key_arg": provider_name,
                    "min_interval_ms_arg": self._min_interval_ms,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PricingProviderRateLimitError(
                f"{provider_name} shared throttle unavailable; retry shortly."
            ) from exc
        finally:
            if should_close:
                client.close()

        row = payload[0] if isinstance(payload, list) and payload else payload
        acquired = bool(row.get("acquired")) if isinstance(row, dict) else False
        retry_after_ms = (
            int(row.get("retry_after_ms") or 1000) if isinstance(row, dict) else 1000
        )
        if not acquired:
            raise PricingProviderRateLimitError(
                f"{provider_name} throttled globally; retry after "
                f"{retry_after_ms / 1000:.2f}s."
            )


class ChainedProviderThrottle:
    """Runs several throttle gates in order."""

    def __init__(self, *throttles) -> None:
        self._throttles = [throttle for throttle in throttles if throttle is not None]

    def acquire(self, provider_name: str) -> None:
        for throttle in self._throttles:
            throttle.acquire(provider_name)
