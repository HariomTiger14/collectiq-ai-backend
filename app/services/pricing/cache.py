import time
from dataclasses import dataclass
from threading import Lock

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
