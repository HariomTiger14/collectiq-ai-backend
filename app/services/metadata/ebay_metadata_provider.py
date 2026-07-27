from __future__ import annotations

import time
from base64 import b64encode
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.schemas.metadata import EbayMetadataResult, EbayMetadataSearchResponse


class EbayMetadataError(Exception):
    """Base exception for eBay metadata provider failures."""


class EbayMetadataUnavailableError(EbayMetadataError):
    """Raised when eBay metadata is not configured or unavailable."""


class EbayMetadataTimeoutError(EbayMetadataError):
    """Raised when eBay metadata requests time out."""


class EbayMetadataRateLimitError(EbayMetadataError):
    """Raised when eBay metadata requests are rate limited."""


@dataclass
class EbayMetadataProvider:
    access_token: str
    client_id: str
    client_secret: str
    browse_api_url: str
    marketplace_id: str
    timeout_seconds: float
    oauth_token_url: str = "https://api.ebay.com/identity/v1/oauth2/token"
    oauth_scope: str = "https://api.ebay.com/oauth/api_scope"
    client: Any = None

    def __post_init__(self) -> None:
        self.access_token = self.access_token.strip()
        self.client_id = self.client_id.strip()
        self.client_secret = self.client_secret.strip()
        self.browse_api_url = self.browse_api_url.strip()
        self.marketplace_id = self.marketplace_id.strip() or "EBAY_AU"
        self.oauth_token_url = self.oauth_token_url.strip()
        self.oauth_scope = self.oauth_scope.strip() or "https://api.ebay.com/oauth/api_scope"
        self._oauth_access_token = ""
        self._oauth_expires_at = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(
            self.browse_api_url
            and (
                self.access_token
                or (self.client_id and self.client_secret)
            )
        )

    def search(self, query: str, limit: int = 10) -> EbayMetadataSearchResponse:
        normalized_query = " ".join(str(query or "").strip().split())
        bounded_limit = max(1, min(limit, 20))
        if len(normalized_query) < 2:
            return EbayMetadataSearchResponse(
                query=normalized_query,
                count=0,
                results=[],
            )
        if not self.is_configured:
            raise EbayMetadataUnavailableError(
                "eBay metadata provider is not configured."
            )
        if not _is_valid_url(self.browse_api_url):
            raise EbayMetadataUnavailableError(
                "EBAY_BROWSE_API_URL is missing or invalid."
            )

        payload = self._request_search(normalized_query, bounded_limit)
        items = payload.get("itemSummaries") or []
        if not isinstance(items, list):
            items = []
        results = [
            _item_to_result(item)
            for item in items
            if isinstance(item, dict) and str(item.get("itemId") or "").strip()
        ][:bounded_limit]
        return EbayMetadataSearchResponse(
            query=normalized_query,
            count=len(results),
            results=results,
        )

    def _request_search(self, query: str, limit: int) -> dict[str, Any]:
        token = self._current_access_token()
        if not token:
            raise EbayMetadataUnavailableError(
                "Configure EBAY_CLIENT_ID/EBAY_CLIENT_SECRET or EBAY_ACCESS_TOKEN."
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
        }
        params = {
            "q": query,
            "limit": str(limit),
        }
        try:
            if self.client is not None:
                response = self.client.get(
                    self.browse_api_url,
                    headers=headers,
                    params=params,
                    timeout=self.timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.get(
                        self.browse_api_url,
                        headers=headers,
                        params=params,
                    )
        except httpx.TimeoutException as exc:
            raise EbayMetadataTimeoutError("eBay metadata request timed out.") from exc
        except httpx.RequestError as exc:
            raise EbayMetadataUnavailableError(
                "eBay metadata request failed before receiving a response."
            ) from exc

        status_code = getattr(response, "status_code", 0)
        if status_code == 429:
            raise EbayMetadataRateLimitError("eBay metadata rate limit reached.")
        if status_code >= 500:
            raise EbayMetadataUnavailableError(
                f"eBay metadata service returned HTTP {status_code}."
            )
        if status_code >= 400:
            raise EbayMetadataError(
                f"eBay metadata request failed with HTTP {status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise EbayMetadataError("eBay metadata response was not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise EbayMetadataError("eBay metadata response shape was invalid.")
        return payload

    def _current_access_token(self) -> str:
        if self.client_id and self.client_secret:
            return self._oauth_application_token()
        return self.access_token

    def _oauth_application_token(self) -> str:
        now = time.time()
        if self._oauth_access_token and now < self._oauth_expires_at:
            return self._oauth_access_token
        if not _is_valid_url(self.oauth_token_url):
            raise EbayMetadataUnavailableError(
                "EBAY_OAUTH_TOKEN_URL is missing or invalid."
            )

        credentials = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        headers = {
            "Authorization": f"Basic {b64encode(credentials).decode('ascii')}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {
            "grant_type": "client_credentials",
            "scope": self.oauth_scope,
        }
        try:
            if self.client is not None:
                response = self.client.post(
                    self.oauth_token_url,
                    headers=headers,
                    data=data,
                    timeout=self.timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.post(
                        self.oauth_token_url,
                        headers=headers,
                        data=data,
                    )
        except httpx.TimeoutException as exc:
            raise EbayMetadataTimeoutError("eBay OAuth token request timed out.") from exc
        except httpx.RequestError as exc:
            raise EbayMetadataUnavailableError(
                "eBay OAuth token request failed before receiving a response."
            ) from exc

        status_code = getattr(response, "status_code", 0)
        if status_code >= 500:
            raise EbayMetadataUnavailableError(
                f"eBay OAuth service returned HTTP {status_code}."
            )
        if status_code >= 400:
            raise EbayMetadataError(
                f"eBay OAuth token request failed with HTTP {status_code}."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise EbayMetadataError("eBay OAuth response was not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise EbayMetadataError("eBay OAuth response shape was invalid.")

        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise EbayMetadataError("eBay OAuth response did not include a token.")
        try:
            expires_in = int(payload.get("expires_in") or 7200)
        except (TypeError, ValueError):
            expires_in = 7200
        self._oauth_access_token = token
        self._oauth_expires_at = now + max(60, expires_in - 60)
        return token


def _item_to_result(item: dict[str, Any]) -> EbayMetadataResult:
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    image = item.get("image") if isinstance(item.get("image"), dict) else {}
    location = (
        item.get("itemLocation")
        if isinstance(item.get("itemLocation"), dict)
        else {}
    )
    return EbayMetadataResult(
        itemId=str(item.get("itemId") or ""),
        title=str(item.get("title") or "eBay item"),
        categoryId=_clean(category.get("categoryId")),
        categoryName=_clean(category.get("categoryName")),
        condition=_clean(item.get("condition")),
        itemWebUrl=_clean(item.get("itemWebUrl")),
        imageUrl=_clean(image.get("imageUrl")),
        itemLocationCountry=_clean(location.get("country")),
        itemCreationDate=_clean(item.get("itemCreationDate")),
        itemEndDate=_clean(item.get("itemEndDate")),
        itemAspects=_aspects(item.get("itemAspects")),
    )


def _aspects(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, list):
        return {}
    aspects: dict[str, list[str]] = {}
    for aspect in value:
        if not isinstance(aspect, dict):
            continue
        name = _clean(aspect.get("name"))
        values = aspect.get("value")
        if not name:
            continue
        if isinstance(values, list):
            clean_values = [
                str(item).strip()
                for item in values
                if str(item or "").strip()
            ]
        elif str(values or "").strip():
            clean_values = [str(values).strip()]
        else:
            clean_values = []
        if clean_values:
            aspects[name] = clean_values
    return aspects


def _clean(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _is_valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

