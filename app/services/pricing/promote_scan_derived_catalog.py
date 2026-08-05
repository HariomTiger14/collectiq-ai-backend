from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from scripts.import_pricecharting_catalog import SupabaseCatalogClient


class ScanDerivedPromotionError(Exception):
    """Raised when scan-derived catalog promotion cannot be completed."""


# Only fields that actually vary for a promoted row feed its content_hash —
# mirrors catalog_history_change_hash()'s role in import_pricecharting_catalog.py,
# scoped to the columns this promotion path can populate.
_PROMOTED_ROW_SIGNATURE_COLUMNS = (
    "product_name",
    "category",
    "currency",
    "market_value_cents",
    "low_estimate_cents",
    "high_estimate_cents",
    "source_provider",
)


@dataclass(frozen=True)
class PromotionResult:
    candidateCount: int
    promotedCount: int
    skippedAlreadyPromoted: int
    skippedMissingTitle: int
    # SCD2 history intentionally not written for promoted rows yet —
    # pricecharting_catalog_history's change-hash signature (CATALOG_HISTORY_
    # SIGNATURE_COLUMNS in import_pricecharting_catalog.py) doesn't cover
    # market_value_cents/low_estimate_cents/high_estimate_cents, so reusing
    # it unmodified would create a history row whose hash never changes even
    # as a scan-derived item's real price moves. Deferred rather than shipped
    # silently broken — see docs/GLOBAL_CATALOG_ARCHITECTURE.md open questions.
    historyRows: int = 0


def promote_scan_derived_rows(
    *,
    min_hit_count: int = 1,
    dry_run: bool = True,
    limit: int = 500,
    timeout_seconds: float = 30,
) -> PromotionResult:
    reader = PricingCacheReader(
        supabase_url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key,
        timeout_seconds=timeout_seconds,
    )
    candidates = reader.fetch_candidates(min_hit_count=min_hit_count, limit=limit)
    already_promoted = reader.fetch_already_promoted_cache_keys()

    rows: list[dict[str, Any]] = []
    skipped_already_promoted = 0
    skipped_missing_title = 0
    for cache_row in candidates:
        cache_key = str(cache_row.get("cache_key") or "").strip()
        if cache_key and cache_key in already_promoted:
            skipped_already_promoted += 1
            continue
        row = to_promoted_catalog_row(cache_row)
        if row is None:
            skipped_missing_title += 1
            continue
        rows.append(row)

    promoted_count = 0
    if not dry_run and rows:
        client = SupabaseCatalogClient(
            supabase_url=settings.supabase_url,
            service_role_key=settings.supabase_service_role_key,
            timeout_seconds=timeout_seconds,
        )
        promoted_count = client.upsert_rows(rows, batch_size=200)

    return PromotionResult(
        candidateCount=len(candidates),
        promotedCount=promoted_count,
        skippedAlreadyPromoted=skipped_already_promoted,
        skippedMissingTitle=skipped_missing_title,
    )


def to_promoted_catalog_row(cache_row: dict[str, Any]) -> dict[str, Any] | None:
    """Maps one pricing_cache_entries row to a pricecharting_catalog row.

    Mirrors to_catalog_row() in scripts/import_pricecharting_catalog.py, fed
    by a scan-derived source instead of a PriceCharting CSV row. Returns None
    when the cache row can't produce a valid catalog row (no cache_key or no
    title — pricecharting_catalog.product_name is NOT NULL).
    """
    cache_key = str(cache_row.get("cache_key") or "").strip()
    title = str(cache_row.get("title") or "").strip()
    if not cache_key or not title:
        return None

    category = str(cache_row.get("category") or "Collectible").strip()
    provider = str(cache_row.get("pricing_provider") or "").strip() or "unknown"

    # original_price/original_currency, NOT value_aud/low_estimate_aud/
    # high_estimate_aud — the *_aud fields are display-currency-converted,
    # frozen at whatever FX rate applied on the scan's request. original_price
    # is the pre-conversion, native-provider value (convert_pricing_result()
    # in currency_conversion.py always sets it before the cache write, so
    # it's reliably genuine — see docs/GLOBAL_CATALOG_ARCHITECTURE.md).
    currency = str(cache_row.get("original_currency") or "USD").strip().upper()
    market_value_cents = _to_cents(cache_row.get("original_price"))

    # No original_low_estimate/original_high_estimate exists in
    # pricing_cache_entries today (see docs/GLOBAL_CATALOG_ARCHITECTURE.md) —
    # left null rather than promoting the AUD-converted low/high range under
    # a native-currency column.
    normalized_identity = (
        str(cache_row.get("normalized_identity") or "").strip()
        or f"{title} {category}".lower().strip()
    )

    row: dict[str, Any] = {
        "pricecharting_id": f"scan:{cache_key}",
        "product_name": title,
        "console_name": None,
        "category": category,
        "upc": None,
        "asin": None,
        "epid": None,
        "release_date": None,
        "loose_price_cents": None,
        "cib_price_cents": None,
        "new_price_cents": None,
        "graded_price_cents": None,
        "box_only_price_cents": None,
        "manual_only_price_cents": None,
        "currency": currency,
        "product_url": None,
        "normalized_identity": normalized_identity,
        "raw_payload": cache_row.get("evidence_json") or {},
        "source_file": None,
        "source_downloaded_at": cache_row.get("checked_at"),
        "source_provider": provider,
        "source_kind": "scan_derived",
        "market_value_cents": market_value_cents,
        "low_estimate_cents": None,
        "high_estimate_cents": None,
        "promoted_from_cache_key": cache_key,
        "verified_at": cache_row.get("checked_at") or _utc_now_iso(),
    }
    row["content_hash"] = _content_hash(row)
    return row


def _content_hash(row: dict[str, Any]) -> str:
    payload = {column: row.get(column) for column in _PROMOTED_ROW_SIGNATURE_COLUMNS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _to_cents(value: Any) -> int | None:
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return round(amount * 100)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PricingCacheReader:
    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        timeout_seconds: float = 30,
        client: httpx.Client | None = None,
    ) -> None:
        self._supabase_url = supabase_url.strip().rstrip("/")
        self._service_role_key = service_role_key.strip()
        self._timeout_seconds = timeout_seconds
        self._client = client
        if not self._supabase_url or not self._service_role_key:
            raise ScanDerivedPromotionError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required."
            )

    def fetch_candidates(self, *, min_hit_count: int, limit: int) -> list[dict[str, Any]]:
        params = {
            "select": "*",
            "hit_count": f"gte.{min_hit_count}",
            "valuation_status": "eq.market_estimated",
            "expires_at": f"gt.{_utc_now_iso()}",
            "title": "not.is.null",
            "order": "hit_count.desc",
            "limit": str(limit),
        }
        payload = self._request("GET", "/rest/v1/pricing_cache_entries", params=params)
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def fetch_already_promoted_cache_keys(self) -> set[str]:
        params = {
            "select": "promoted_from_cache_key",
            "promoted_from_cache_key": "not.is.null",
        }
        payload = self._request("GET", "/rest/v1/pricecharting_catalog", params=params)
        if not isinstance(payload, list):
            return set()
        return {
            str(row.get("promoted_from_cache_key"))
            for row in payload
            if isinstance(row, dict) and row.get("promoted_from_cache_key")
        }

    def _request(self, method: str, path: str, *, params: dict[str, str]) -> Any:
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
        }
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.request(
                method,
                f"{self._supabase_url}{path}",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ScanDerivedPromotionError("Pricing cache request failed.") from error
        finally:
            if should_close:
                client.close()
