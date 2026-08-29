from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.services.pricing.marketplace_listing_filters import (
    has_meaningful_title_overlap,
    is_junk_listing_title,
)


class PriceChartingListingService:
    # PriceCharting's Marketplace API (/api/offers) -- confirmed live to
    # need only the same PRICECHARTING_API_KEY already used for the Prices
    # API (no separate Marketplace API entitlement; the "API access" scope
    # on their Legendary plan covers both, live-verified via a real
    # request before this was built). Unlike eBay's keyword/GTIN search,
    # querying by `id=<PriceCharting's own product id>` is an exact
    # product match, not a fuzzy one -- our own catalog_id IS that same
    # id (pricecharting_catalog.pricecharting_id), so there's no
    # ambiguity to resolve on this source's side. The junk/overlap filters
    # below still run anyway, as a defensive safety net (a mislisted
    # "lot" under the right product id is still possible), not because
    # this source is expected to need them the way eBay's search does.
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = (api_key if api_key is not None else settings.pricecharting_api_key).strip()
        self._api_base = (
            api_base if api_base is not None else settings.pricecharting_api_base
        ).strip().rstrip("/")
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.pricecharting_timeout_seconds
        )
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._api_base)

    def get_offers(
        self, product_id: str, *, catalog_title: str, limit: int = 3
    ) -> list[dict[str, Any]]:
        normalized_id = product_id.strip()
        if not normalized_id or not self.is_configured:
            return []
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.get(
                f"{self._api_base}/api/offers",
                params={
                    "t": self._api_key,
                    "id": normalized_id,
                    "status": "available",
                    "sort": "lowest-price",
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError:
            return []
        finally:
            if should_close:
                client.close()
        if response.status_code >= 400:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        if not isinstance(payload, dict) or payload.get("status") != "success":
            return []
        offers = payload.get("offers")
        if not isinstance(offers, list):
            return []
        candidates = [row for row in (_offer_to_listing(offer) for offer in offers) if row is not None]
        relevant = [
            row
            for row in candidates
            if not is_junk_listing_title(row["title"])
            and has_meaningful_title_overlap(catalog_title, row["title"])
        ]
        return relevant[:limit]


def _offer_to_listing(offer: Any) -> dict[str, Any] | None:
    if not isinstance(offer, dict):
        return None
    title = offer.get("product-name")
    offer_url = offer.get("offer-url")
    price_cents = offer.get("price")
    if not title or not offer_url:
        return None
    try:
        price = round(float(price_cents) / 100, 2)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    full_url = str(offer_url)
    if full_url.startswith("/"):
        full_url = f"https://www.pricecharting.com{full_url}"
    return {
        "title": str(title),
        "price": price,
        # PriceCharting's Marketplace API doesn't return a currency field
        # at all -- it's a single, US-based marketplace, always USD.
        # Unlike eBay (which has a real per-region marketplace, so
        # currency conversion happens by picking the right marketplace),
        # this needs actual conversion applied by the caller when a
        # non-USD display currency was requested -- see
        # CatalogSearchService._fetch_pricecharting_listings.
        "currency": "USD",
        "condition": str(offer.get("condition-string") or ""),
        "url": full_url,
        "source": "PriceCharting",
    }
