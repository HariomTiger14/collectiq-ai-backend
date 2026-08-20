from __future__ import annotations

import time
from base64 import b64encode
from typing import Any

import httpx

from app.core.config import settings


# Maps PackLox's 4 supported display currencies (CollectorProfile.
# preferredCurrency on the mobile app, same set as currency_conversion.py's
# SUPPORTED_DISPLAY_CURRENCIES) to eBay's own marketplace ID enum. Each
# entry live-confirmed against the real Buy Browse API before shipping:
# every marketplace below actually returns results, in the matching native
# currency, with no client-side currency conversion needed as a result.
CURRENCY_TO_EBAY_MARKETPLACE: dict[str, str] = {
    "AUD": "EBAY_AU",
    "USD": "EBAY_US",
    "CAD": "EBAY_CA",
    "GBP": "EBAY_GB",
}
DEFAULT_EBAY_MARKETPLACE = "EBAY_AU"


def ebay_marketplace_for_currency(currency: str | None) -> str:
    normalized = (currency or "").strip().upper()
    return CURRENCY_TO_EBAY_MARKETPLACE.get(normalized, DEFAULT_EBAY_MARKETPLACE)


class EbayListingServiceError(Exception):
    """Raised when eBay listings cannot be fetched. Always caught by the
    caller (CatalogSearchService) -- a live-listings enrichment failing
    must never break the rest of the catalog detail response."""


class EbayListingService:
    # Uses the Buy Browse API's public-data scope only (https://api.ebay.
    # com/oauth/api_scope, "View public data from eBay") -- confirmed live
    # against the Production OAuth Scopes page that this scope is granted
    # under the Client Credential Grant Type, independent of the separate,
    # explicitly-denied Marketplace Insights API access (a different scope
    # entirely, used only by EbayPricingProvider for sold-comps pricing).
    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        oauth_token_url: str | None = None,
        oauth_scope: str = "https://api.ebay.com/oauth/api_scope",
        browse_api_url: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._client_id = (client_id if client_id is not None else settings.ebay_client_id).strip()
        self._client_secret = (
            client_secret if client_secret is not None else settings.ebay_client_secret
        ).strip()
        self._oauth_token_url = (
            oauth_token_url if oauth_token_url is not None else settings.ebay_oauth_token_url
        ).strip()
        self._oauth_scope = oauth_scope
        self._browse_api_url = (
            browse_api_url if browse_api_url is not None else settings.ebay_browse_api_url
        ).strip()
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.ebay_timeout_seconds
        )
        self._client = client
        self._token = ""
        self._token_expires_at = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._oauth_token_url)

    def search_listings(
        self, query: str, *, marketplace_id: str, limit: int = 3
    ) -> list[dict[str, Any]]:
        normalized_query = query.strip()
        if not normalized_query or not self.is_configured:
            return []
        token = self._access_token()
        if not token:
            return []
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.get(
                self._browse_api_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": marketplace_id,
                },
                params={"q": normalized_query, "limit": str(max(1, min(limit, 10)))},
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
        items = payload.get("itemSummaries") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [row for row in (_item_to_listing(item) for item in items) if row is not None]

    def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at:
            return self._token
        credentials = f"{self._client_id}:{self._client_secret}".encode("utf-8")
        basic_token = b64encode(credentials).decode("ascii")
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.post(
                self._oauth_token_url,
                headers={
                    "Authorization": f"Basic {basic_token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={"grant_type": "client_credentials", "scope": self._oauth_scope},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError:
            return ""
        finally:
            if should_close:
                client.close()
        if response.status_code >= 400:
            return ""
        try:
            payload = response.json()
        except ValueError:
            return ""
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            return ""
        expires_in = payload.get("expires_in")
        try:
            ttl_seconds = int(expires_in)
        except (TypeError, ValueError):
            ttl_seconds = 3600
        # Refresh a bit before the real expiry so a request never races a
        # token that expires mid-flight.
        self._token = str(token)
        self._token_expires_at = now + max(60, ttl_seconds - 120)
        return self._token


def _item_to_listing(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    url = item.get("itemWebUrl")
    price_payload = item.get("price")
    if not title or not url or not isinstance(price_payload, dict):
        return None
    try:
        price = round(float(price_payload.get("value")), 2)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return {
        "title": str(title),
        "price": price,
        "currency": str(price_payload.get("currency") or "").upper(),
        "condition": str(item.get("condition") or ""),
        "url": str(url),
    }
