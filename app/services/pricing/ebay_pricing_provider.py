import logging
import time
from base64 import b64encode
from dataclasses import replace
from urllib.parse import urlparse

import httpx

from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.base_pricing_provider import (
    EmptyMarketDataError,
    MarketComparableSale,
    PricingProvider,
    PricingProviderError,
    PricingProviderRateLimitError,
    PricingProviderTimeoutError,
    PricingProviderUnavailableError,
    PricingResult,
    utc_timestamp,
)
from app.services.pricing.cache import InMemoryPricingCache, ProviderThrottle


logger = logging.getLogger("collectiq.pricing.ebay")


class EbayPricingProvider(PricingProvider):
    provider_name = "ebay"
    SOLD_COMPS_SOURCE = "eBay sold comps"
    ACTIVE_LISTING_SOURCE = "eBay active listing signal"

    def __init__(
        self,
        *,
        access_token: str,
        browse_api_url: str,
        marketplace_id: str,
        timeout_seconds: float,
        cache_ttl_seconds: int,
        min_interval_ms: int,
        marketplace_insights_api_url: str = "",
        client_id: str = "",
        client_secret: str = "",
        oauth_token_url: str = "https://api.ebay.com/identity/v1/oauth2/token",
        oauth_scope: str = "https://api.ebay.com/oauth/api_scope",
        browse_min_results: int = 3,
        client=None,
        cache: InMemoryPricingCache | None = None,
        throttle: ProviderThrottle | None = None,
    ) -> None:
        self._access_token = access_token.strip()
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._oauth_token_url = oauth_token_url.strip()
        self._oauth_scope = oauth_scope.strip() or "https://api.ebay.com/oauth/api_scope"
        self._oauth_access_token = ""
        self._oauth_expires_at = 0.0
        self._browse_api_url = browse_api_url.strip()
        self._marketplace_insights_api_url = marketplace_insights_api_url.strip()
        self._marketplace_id = marketplace_id.strip() or "EBAY_AU"
        self._timeout_seconds = timeout_seconds
        self._browse_min_results = max(1, browse_min_results)
        self._client = client
        self._cache = cache or InMemoryPricingCache(cache_ttl_seconds)
        self._throttle = throttle or ProviderThrottle(min_interval_ms)

    def price(self, recognition: RecognitionResult) -> PricingResult:
        access_token = self._current_access_token()
        if not access_token:
            raise PricingProviderUnavailableError(
                "Configure EBAY_CLIENT_ID/EBAY_CLIENT_SECRET or EBAY_ACCESS_TOKEN."
            )
        if not self._has_sold_comps_endpoint():
            raise PricingProviderUnavailableError(
                "eBay sold-comps unavailable: partner access not granted. "
                "Active listing asking prices are not used for PackLox valuations."
            )

        query = self._query_for(recognition)
        market_mode = "sold"
        cache_key = self._cache_key(recognition, query, market_mode)
        cached = self._cache.get(cache_key)
        if isinstance(cached, PricingResult):
            return replace(
                cached,
                cacheStatus="hit",
                providerDiagnostics={
                    **cached.providerDiagnostics,
                    "cacheStatus": "hit",
                    "provider": self.provider_name,
                },
            )

        self._throttle.acquire(self.provider_name)
        started_at = time.perf_counter()
        response: dict
        market_data_type = "sold_comps"
        sold_error = ""
        try:
            response = self._request(query, self._marketplace_insights_api_url)
        except PricingProviderError:
            raise
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        result = self._parse_response(
            recognition=recognition,
            payload=response,
            latency_ms=latency_ms,
            market_data_type=market_data_type,
            fallback_reason=sold_error,
        )
        self._cache.set(cache_key, result)
        return result

    def _current_access_token(self) -> str:
        if self._client_id and self._client_secret:
            return self._oauth_application_token()
        return self._access_token

    def _oauth_application_token(self) -> str:
        now = time.time()
        if self._oauth_access_token and now < self._oauth_expires_at:
            return self._oauth_access_token
        if not self._is_valid_url(self._oauth_token_url):
            raise PricingProviderUnavailableError(
                "EBAY_OAUTH_TOKEN_URL is missing or invalid."
            )

        credentials = f"{self._client_id}:{self._client_secret}".encode("utf-8")
        basic_token = b64encode(credentials).decode("ascii")
        headers = {
            "Authorization": f"Basic {basic_token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {
            "grant_type": "client_credentials",
            "scope": self._oauth_scope,
        }

        try:
            if self._client is not None:
                response = self._client.post(
                    self._oauth_token_url,
                    headers=headers,
                    data=data,
                    timeout=self._timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self._timeout_seconds) as client:
                    response = client.post(
                        self._oauth_token_url,
                        headers=headers,
                        data=data,
                    )
        except httpx.TimeoutException as exc:
            raise PricingProviderTimeoutError(
                "eBay OAuth token request timed out."
            ) from exc
        except httpx.RequestError as exc:
            raise PricingProviderUnavailableError(
                "eBay OAuth token request failed before receiving a response."
            ) from exc

        status_code = getattr(response, "status_code", 0)
        if status_code >= 500:
            raise PricingProviderUnavailableError(
                f"eBay OAuth service returned HTTP {status_code}."
            )
        if status_code >= 400:
            raise PricingProviderError(
                f"eBay OAuth token request failed with HTTP {status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PricingProviderError("eBay OAuth response was not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise PricingProviderError("eBay OAuth response shape was invalid.")

        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise PricingProviderError("eBay OAuth response did not include a token.")
        try:
            expires_in = int(payload.get("expires_in") or 7200)
        except (TypeError, ValueError):
            expires_in = 7200

        self._oauth_access_token = token
        self._oauth_expires_at = now + max(60, expires_in - 60)
        return token

    def _request(self, query: str, api_url: str) -> dict:
        headers = {
            "Authorization": f"Bearer {self._current_access_token()}",
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": self._marketplace_id,
        }
        params = {
            "q": query,
            "limit": "20",
        }

        try:
            if self._client is not None:
                response = self._client.get(
                    api_url,
                    headers=headers,
                    params=params,
                    timeout=self._timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self._timeout_seconds) as client:
                    response = client.get(
                        api_url,
                        headers=headers,
                        params=params,
                    )
        except httpx.TimeoutException as exc:
            raise PricingProviderTimeoutError("eBay pricing request timed out.") from exc
        except httpx.RequestError as exc:
            raise PricingProviderUnavailableError(
                "eBay pricing request failed before receiving a response."
            ) from exc

        status_code = getattr(response, "status_code", 0)
        if status_code == 429:
            raise PricingProviderRateLimitError("eBay pricing rate limit reached.")
        if status_code >= 500:
            raise PricingProviderUnavailableError(
                f"eBay pricing service returned HTTP {status_code}."
            )
        if status_code >= 400:
            raise PricingProviderError(
                f"eBay pricing request failed with HTTP {status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PricingProviderError("eBay pricing response was not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise PricingProviderError("eBay pricing response shape was invalid.")
        return payload

    def _parse_response(
        self,
        *,
        recognition: RecognitionResult,
        payload: dict,
        latency_ms: int,
        market_data_type: str,
        fallback_reason: str = "",
    ) -> PricingResult:
        items = self._items_from_payload(payload, market_data_type)
        if not isinstance(items, list) or not items:
            raise EmptyMarketDataError("eBay returned no pricing results.")

        comparable_sales: list[MarketComparableSale] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            sale = self._sale_from_item(item, recognition, market_data_type)
            if sale is not None:
                comparable_sales.append(sale)

        if not comparable_sales:
            raise EmptyMarketDataError("eBay returned no usable pricing results.")

        prices = [sale.soldPrice for sale in comparable_sales]
        estimated_value = round(sum(prices) / len(prices))
        low_estimate = min(prices)
        high_estimate = max(prices)
        confidence = self._confidence(
            recognition,
            len(comparable_sales),
            market_data_type=market_data_type,
        )
        source = self.SOLD_COMPS_SOURCE

        return PricingResult(
            estimatedMarketValue=max(1, estimated_value),
            lowEstimate=max(1, low_estimate),
            highEstimate=max(1, high_estimate),
            currency=comparable_sales[0].currency,
            pricingSource=source,
            pricingConfidence=confidence,
            lastUpdated=utc_timestamp(),
            marketTrend=self._trend(comparable_sales),
            sourceCount=1,
            pricingAge="sold_comps",
            comparableSales=comparable_sales,
            cacheStatus="miss",
            providerDiagnostics={
                "provider": self.provider_name,
                "cacheStatus": "miss",
                "responseLatencyMs": str(latency_ms),
                "pricingFreshness": "sold_comps",
                "fallbackReason": fallback_reason,
                "resultCount": str(len(comparable_sales)),
                "marketDataType": market_data_type,
                "trustNote": "Sold/completed marketplace data",
            },
        )

    def _items_from_payload(self, payload: dict, market_data_type: str) -> list:
        if market_data_type == "sold_comps":
            items = (
                payload.get("itemSales")
                or payload.get("soldItems")
                or payload.get("completedItems")
                or payload.get("itemSummaries")
                or payload.get("items")
                or []
            )
        else:
            items = payload.get("itemSummaries") or payload.get("items") or []
        return items if isinstance(items, list) else []

    def _sale_from_item(
        self,
        item: dict,
        recognition: RecognitionResult,
        market_data_type: str,
    ) -> MarketComparableSale | None:
        price_payload = (
            item.get("price")
            or item.get("itemSoldPrice")
            or item.get("soldPrice")
            or item.get("currentBidPrice")
            or {}
        )
        if not isinstance(price_payload, dict):
            return None
        try:
            price = round(float(price_payload.get("value")))
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None

        currency = str(price_payload.get("currency") or "AUD").upper()
        source = self.SOLD_COMPS_SOURCE
        return MarketComparableSale(
            source=source,
            title=str(item.get("title") or recognition.title),
            soldPrice=price,
            currency=currency,
            soldDate=str(
                item.get("itemSoldDate")
                or item.get("soldDate")
                or item.get("itemEndDate")
                or item.get("itemCreationDate")
                or utc_timestamp()
            ),
            condition=str(item.get("condition") or recognition.condition or "Unknown"),
            url=item.get("itemWebUrl"),
        )

    def _query_for(self, recognition: RecognitionResult) -> str:
        parts = [
            recognition.title,
            recognition.brand,
            recognition.setName,
            recognition.series,
            recognition.cardNumber,
            recognition.year,
            recognition.edition,
            recognition.language,
            recognition.condition,
        ]
        query = " ".join(
            str(part).strip()
            for part in parts
            if isinstance(part, str) and part.strip()
        )
        return query or recognition.category or "collectible"

    def _cache_key(
        self,
        recognition: RecognitionResult,
        query: str,
        market_mode: str,
    ) -> str:
        identity = "|".join(
            [
                self.provider_name,
                market_mode,
                query,
                recognition.category,
                recognition.condition,
            ]
        )
        return " ".join(identity.lower().split())

    def _confidence(
        self,
        recognition: RecognitionResult,
        comparable_count: int,
        *,
        market_data_type: str,
    ) -> int:
        comp_bonus = min(20, comparable_count * 4)
        base = min(85, max(45, round(recognition.confidence * 0.75)))
        confidence = max(40, min(92, base + comp_bonus))
        return confidence

    def _trend(self, comparable_sales: list[MarketComparableSale]) -> str:
        if len(comparable_sales) < 3:
            return "Stable"
        ordered = sorted(comparable_sales, key=lambda sale: sale.soldDate)
        first = ordered[0].soldPrice
        last = ordered[-1].soldPrice
        if last >= first * 1.08:
            return "Rising"
        if last <= first * 0.92:
            return "Cooling"
        return "Stable"

    def _is_valid_url(self, value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def _has_sold_comps_endpoint(self) -> bool:
        return self._is_valid_url(self._marketplace_insights_api_url)
