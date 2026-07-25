from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.search import (
    CatalogSearchPricing,
    CatalogSearchResponse,
    CatalogSearchResult,
)


class CatalogSearchError(Exception):
    """Raised when catalog search cannot be completed."""


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
                "source_downloaded_at,updated_at,normalized_identity"
            ),
            "or": (
                f"(product_name.ilike.{pattern},"
                f"console_name.ilike.{pattern},"
                f"category.ilike.{pattern},"
                f"upc.ilike.{pattern},"
                f"normalized_identity.ilike.{pattern})"
            ),
            "limit": str(min(max(limit * 3, limit), 100)),
        }
        payload = self._request("GET", "/rest/v1/pricecharting_catalog", params=params)
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


def _row_to_result(row: dict[str, Any], query: str) -> CatalogSearchResult:
    pricing = _pricing_from_row(row)
    return CatalogSearchResult(
        id=str(row.get("pricecharting_id") or ""),
        title=str(row.get("product_name") or "Catalog item"),
        category=str(row.get("category") or row.get("console_name") or "Catalog"),
        source="PriceCharting",
        setName=_clean(row.get("console_name")),
        identifier=_clean(row.get("upc")),
        productUrl=_clean(row.get("product_url")),
        sourceFile=_clean(row.get("source_file")),
        confidence=_match_confidence(row, query),
        attribution="Pricing data by PriceCharting",
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
