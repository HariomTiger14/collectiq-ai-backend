import time
from typing import Any

import httpx


DEFAULT_CACHE_TTL_SECONDS = 3600
DEFAULT_ROW_LIMIT = 5000

_SELECT_COLUMNS = (
    "kicksdb_id,sku,slug,title,brand,model,product_url,currency,"
    "min_price_cents,max_price_cents,avg_price_cents,variants"
)


class KicksDBCatalogMatcher:
    """Holds the backfilled kicksdb_catalog table in memory so scan-time
    lookups can check it before making a live KicksDB API call.

    The catalog is small (low thousands of rows), so caching the whole
    thing in-process and refreshing periodically is both simpler and
    faster than a per-scan Supabase round-trip -- there's no reason a
    catalog lookup should add network latency to the hot scan path when
    the whole table fits comfortably in memory.
    """

    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        timeout_seconds: float,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        row_limit: int = DEFAULT_ROW_LIMIT,
        client: httpx.Client | None = None,
    ) -> None:
        self._supabase_url = supabase_url.strip().rstrip("/")
        self._service_role_key = service_role_key.strip()
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._row_limit = row_limit
        self._client = client
        self._rows: list[dict[str, Any]] | None = None
        self._loaded_at: float = 0.0

    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def candidates(self) -> list[dict[str, Any]]:
        if not self.is_configured():
            return []
        now = time.monotonic()
        if self._rows is None or (now - self._loaded_at) > self._cache_ttl_seconds:
            self._rows = self._load_rows()
            self._loaded_at = now
        return self._rows

    def _load_rows(self) -> list[dict[str, Any]]:
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.get(
                f"{self._supabase_url}/rest/v1/kicksdb_catalog",
                params={"select": _SELECT_COLUMNS, "limit": str(self._row_limit)},
                headers={
                    "apikey": self._service_role_key,
                    "Authorization": f"Bearer {self._service_role_key}",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                return []
            return [row for row in payload if isinstance(row, dict)]
        except (httpx.HTTPError, ValueError):
            # A catalog-load failure just means every scan falls through to
            # the existing live-lookup path this run -- not worth raising
            # and breaking pricing entirely over a cache warm-up hiccup.
            return []
        finally:
            if should_close:
                client.close()
