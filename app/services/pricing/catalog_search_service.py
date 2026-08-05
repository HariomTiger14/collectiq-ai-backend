from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.search import (
    CatalogDetailResponse,
    CatalogHistoryPoint,
    CatalogSearchPricing,
    CatalogSearchResponse,
    CatalogSearchResult,
)


class CatalogSearchError(Exception):
    """Raised when catalog search cannot be completed."""


class CatalogItemNotFoundError(CatalogSearchError):
    """Raised when a catalog item cannot be found."""


@dataclass(frozen=True)
class CatalogSearchService:
    supabase_url: str | None = None
    service_role_key: str | None = None
    timeout_seconds: float = 5
    client: httpx.Client | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def search(self, query: str, limit: int = 20) -> CatalogSearchResponse:
        normalized_query = _normalize_query(query)
        bounded_limit = max(1, min(limit, 50))
        if len(normalized_query) < 2:
            return CatalogSearchResponse(query=normalized_query, count=0, results=[])
        if not self.is_configured:
            raise CatalogSearchError("Catalog search is not configured.")

        rows = self._fetch_rows(normalized_query, bounded_limit)
        results = [
            _row_to_result(row, normalized_query)
            for row in _rank_rows(rows, normalized_query)
        ][:bounded_limit]
        return CatalogSearchResponse(
            query=normalized_query,
            count=len(results),
            results=results,
        )

    def detail(self, catalog_id: str, history_limit: int = 30) -> CatalogDetailResponse:
        normalized_id = str(catalog_id or "").strip()
        bounded_history_limit = max(1, min(history_limit, 90))
        if not normalized_id:
            raise CatalogItemNotFoundError("Catalog item was not found.")
        if not self.is_configured:
            raise CatalogSearchError("Catalog search is not configured.")

        row = self._fetch_catalog_row(normalized_id)
        if row is None:
            raise CatalogItemNotFoundError("Catalog item was not found.")

        history_rows = self._fetch_history_rows(normalized_id, bounded_history_limit)
        return CatalogDetailResponse(
            result=_row_to_result(
                row,
                _normalize_query(str(row.get("product_name") or "")),
            ),
            history=[_history_row_to_point(row) for row in history_rows],
        )

    @property
    def _supabase_url(self) -> str:
        value = self.supabase_url if self.supabase_url is not None else settings.supabase_url
        return value.strip().rstrip("/")

    @property
    def _service_role_key(self) -> str:
        value = (
            self.service_role_key
            if self.service_role_key is not None
            else settings.supabase_service_role_key
        )
        return value.strip()

    def _fetch_rows(self, query: str, limit: int) -> list[dict[str, Any]]:
        pattern = _postgrest_ilike_pattern(query)
        params = {
            "select": (
                "pricecharting_id,product_name,console_name,category,upc,"
                "loose_price_cents,cib_price_cents,new_price_cents,"
                "graded_price_cents,currency,product_url,source_file,"
                "source_downloaded_at,updated_at,normalized_identity,"
                "source_provider,market_value_cents,low_estimate_cents,"
                "high_estimate_cents"
            ),
            "or": (
                f"(product_name.ilike.{pattern},"
                f"console_name.ilike.{pattern},"
                f"category.ilike.{pattern},"
                f"upc.ilike.{pattern},"
                f"normalized_identity.ilike.{pattern})"
            ),
            # Ranking happens in Python (_rank_rows) over whatever subset this
            # request returns — without an explicit order, PostgreSQL doesn't
            # guarantee any particular row order, so identical repeated
            # queries could return a different subset each time (seen live:
            # a promoted row present in one call's top results, absent from
            # the next). product_name.asc makes the DB-level fetch
            # deterministic call to call, and as a side effect tends to sort
            # a short exact-ish title ("Pikachu V") ahead of longer variants
            # sharing its prefix ("Pikachu V #1", "Pikachu V #10", ...).
            # This does NOT guarantee the true best match is always inside
            # the fetch window for a query matching hundreds of rows — a real
            # fix needs DB-side relevance ranking (e.g. the existing
            # pricecharting_catalog_search_idx GIN/tsvector index via an RPC),
            # which is a separate, larger change, not done here.
            "order": "product_name.asc",
            "limit": str(min(max(limit * 5, limit), 200)),
        }
        payload = self._request("GET", "/rest/v1/pricecharting_catalog", params=params)
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _fetch_catalog_row(self, catalog_id: str) -> dict[str, Any] | None:
        params = {
            "select": (
                "pricecharting_id,product_name,console_name,category,upc,"
                "loose_price_cents,cib_price_cents,new_price_cents,"
                "graded_price_cents,box_only_price_cents,manual_only_price_cents,"
                "currency,product_url,source_file,source_downloaded_at,"
                "updated_at,normalized_identity,"
                "source_provider,market_value_cents,low_estimate_cents,"
                "high_estimate_cents"
            ),
            "pricecharting_id": f"eq.{catalog_id}",
            "limit": "1",
        }
        payload = self._request("GET", "/rest/v1/pricecharting_catalog", params=params)
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        return row if isinstance(row, dict) else None

    def _fetch_history_rows(self, catalog_id: str, limit: int) -> list[dict[str, Any]]:
        params = {
            "select": (
                "valid_from,valid_to,is_current,source_file,source_downloaded_at,"
                "loose_price_cents,cib_price_cents,new_price_cents,"
                "graded_price_cents,box_only_price_cents,manual_only_price_cents,"
                "currency"
            ),
            "pricecharting_id": f"eq.{catalog_id}",
            "order": "valid_from.desc",
            "limit": str(limit),
        }
        payload = self._request(
            "GET",
            "/rest/v1/pricecharting_catalog_history",
            params=params,
        )
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        client = self.client or httpx.Client(timeout=self.timeout_seconds)
        should_close = self.client is None
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
            raise CatalogSearchError("Catalog search request failed.") from error
        finally:
            if should_close:
                client.close()


_PROVIDER_DISPLAY_NAMES = {
    "pricecharting_import": "PriceCharting",
    "pricecharting": "PriceCharting",
    "kicksdb": "KicksDB",
    "tcgplayer": "TCGPlayer",
    "ebay": "eBay",
}


def _source_display_name(row: dict[str, Any]) -> str:
    provider = str(row.get("source_provider") or "").strip().lower()
    return _PROVIDER_DISPLAY_NAMES.get(provider, "PriceCharting")


def _row_to_result(row: dict[str, Any], query: str) -> CatalogSearchResult:
    pricing = _pricing_from_row(row)
    source = _source_display_name(row)
    return CatalogSearchResult(
        id=str(row.get("pricecharting_id") or ""),
        title=str(row.get("product_name") or "Catalog item"),
        category=str(row.get("category") or row.get("console_name") or "Catalog"),
        source=source,
        setName=_clean(row.get("console_name")),
        identifier=_clean(row.get("upc")),
        productUrl=_clean(row.get("product_url")),
        sourceFile=_clean(row.get("source_file")),
        confidence=_match_confidence(row, query),
        attribution=f"Pricing data by {source}",
        lastUpdated=_latest_timestamp(row),
        imageUrl=None,
        pricing=pricing,
    )


def _pricing_from_row(row: dict[str, Any]) -> CatalogSearchPricing:
    loose = _cents_to_units(row.get("loose_price_cents"))
    cib = _cents_to_units(row.get("cib_price_cents"))
    new = _cents_to_units(row.get("new_price_cents"))
    graded = _cents_to_units(row.get("graded_price_cents"))
    prices = [price for price in [loose, cib, new, graded] if price is not None and price > 0]
    market_value = loose or cib or new or graded
    low = min(prices) if prices else None
    high = max(prices) if prices else None

    if market_value is None:
        # Scan-derived rows (source_kind='scan_derived') don't populate the
        # PriceCharting-specific price tiers above — fall back to the
        # provider-neutral market_value_cents/low/high_estimate_cents columns
        # instead (see docs/GLOBAL_CATALOG_ARCHITECTURE.md).
        market_value = _cents_to_units(row.get("market_value_cents"))
        low = _cents_to_units(row.get("low_estimate_cents"))
        high = _cents_to_units(row.get("high_estimate_cents"))

    return CatalogSearchPricing(
        currency=str(row.get("currency") or "USD").upper(),
        marketValue=market_value,
        lowEstimate=low,
        highEstimate=high,
        loosePrice=loose,
        cibPrice=cib,
        newPrice=new,
        gradedPrice=graded,
    )


def _history_row_to_point(row: dict[str, Any]) -> CatalogHistoryPoint:
    return CatalogHistoryPoint(
        validFrom=str(row.get("valid_from") or ""),
        validTo=_clean(row.get("valid_to")),
        isCurrent=bool(row.get("is_current")),
        sourceFile=_clean(row.get("source_file")),
        sourceDownloadedAt=_clean(row.get("source_downloaded_at")),
        pricing=_pricing_from_row(row),
    )


def _rank_rows(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (-_match_score(row, query), str(row.get("product_name") or "")))


def _match_confidence(row: dict[str, Any], query: str) -> float:
    score = _match_score(row, query)
    if score >= 100:
        return 0.96
    if score >= 80:
        return 0.90
    if score >= 55:
        return 0.78
    return 0.62


def _match_score(row: dict[str, Any], query: str) -> int:
    product = str(row.get("product_name") or "").lower()
    console = str(row.get("console_name") or "").lower()
    category = str(row.get("category") or "").lower()
    upc = str(row.get("upc") or "").lower()
    identity = str(row.get("normalized_identity") or "").lower()
    if query == product or query == upc:
        return 110
    if product.startswith(query):
        return 95
    if query in product:
        return 80
    if query in identity:
        return 70
    if query in console or query in category:
        return 55
    return 25


def _normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())


def _postgrest_ilike_pattern(query: str) -> str:
    safe = query.replace(",", " ").replace("(", " ").replace(")", " ").strip()
    safe = " ".join(safe.split())
    return f"*{safe}*"


def _cents_to_units(value: Any) -> float | None:
    try:
        if value is None:
            return None
        cents = int(value)
    except (TypeError, ValueError):
        return None
    if cents <= 0:
        return None
    return round(cents / 100, 2)


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _latest_timestamp(row: dict[str, Any]) -> str | None:
    for key in ("source_downloaded_at", "updated_at"):
        value = _clean(row.get(key))
        if value:
            return value
    return datetime.utcnow().isoformat() + "Z"
