import logging
import time
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

    def __init__(
        self,
        *,
        access_token: str,
        browse_api_url: str,
        marketplace_id: str,
        timeout_seconds: float,
        cache_ttl_seconds: int,
        min_interval_ms: int,
        client=None,
        cache: InMemoryPricingCache | None = None,
        throttle: ProviderThrottle | None = None,
    ) -> None:
        self._access_token = access_token.strip()
        self._browse_api_url = browse_api_url.strip()
        self._marketplace_id = marketplace_id.strip() or "EBAY_AU"
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._cache = cache or InMemoryPricingCache(cache_ttl_seconds)
        self._throttle = throttle or ProviderThrottle(min_interval_ms)

    def price(self, recognition: RecognitionResult) -> PricingResult:
        if not self._access_token:
            raise PricingProviderUnavailableError(
                "EBAY_ACCESS_TOKEN is not configured on the backend."
            )
        if not self._is_valid_url(self._browse_api_url):
            raise PricingProviderUnavailableError(
                "EBAY_BROWSE_API_URL is missing or invalid."
            )

        query = self._query_for(recognition)
        cache_key = self._cache_key(recognition, query)
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
        response = self._request(query)
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        result = self._parse_response(
            recognition=recognition,
            payload=response,
            latency_ms=latency_ms,
        )
        self._cache.set(cache_key, result)
        return result

    def _request(self, query: str) -> dict:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
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
                    self._browse_api_url,
                    headers=headers,
                    params=params,
                    timeout=self._timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self._timeout_seconds) as client:
                    response = client.get(
                        self._browse_api_url,
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
    ) -> PricingResult:
        items = payload.get("itemSummaries") or payload.get("items") or []
        if not isinstance(items, list) or not items:
            raise EmptyMarketDataError("eBay returned no pricing results.")

        comparable_sales: list[MarketComparableSale] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            sale = self._sale_from_item(item, recognition)
            if sale is not None:
                comparable_sales.append(sale)

        if not comparable_sales:
            raise EmptyMarketDataError("eBay returned no usable pricing results.")

        prices = [sale.soldPrice for sale in comparable_sales]
        estimated_value = round(sum(prices) / len(prices))
        low_estimate = min(prices)
        high_estimate = max(prices)
        confidence = self._confidence(recognition, len(comparable_sales))

        return PricingResult(
            estimatedMarketValue=max(1, estimated_value),
            lowEstimate=max(1, low_estimate),
            highEstimate=max(1, high_estimate),
            currency=comparable_sales[0].currency,
            pricingSource="eBay Browse API",
            pricingConfidence=confidence,
            lastUpdated=utc_timestamp(),
            marketTrend=self._trend(comparable_sales),
            sourceCount=1,
            pricingAge="live",
            comparableSales=comparable_sales,
            cacheStatus="miss",
            providerDiagnostics={
                "provider": self.provider_name,
                "cacheStatus": "miss",
                "responseLatencyMs": str(latency_ms),
                "pricingFreshness": "live",
                "fallbackReason": "",
                "resultCount": str(len(comparable_sales)),
            },
        )

    def _sale_from_item(
        self,
        item: dict,
        recognition: RecognitionResult,
    ) -> MarketComparableSale | None:
        price_payload = item.get("price") or item.get("currentBidPrice") or {}
        if not isinstance(price_payload, dict):
            return None
        try:
            price = round(float(price_payload.get("value")))
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None

        currency = str(price_payload.get("currency") or "AUD").upper()
        return MarketComparableSale(
            source="eBay Browse API",
            title=str(item.get("title") or recognition.title),
            soldPrice=price,
            currency=currency,
            soldDate=str(
                item.get("itemCreationDate")
                or item.get("itemEndDate")
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

    def _cache_key(self, recognition: RecognitionResult, query: str) -> str:
        identity = "|".join(
            [
                self.provider_name,
                query,
                recognition.category,
                recognition.condition,
            ]
        )
        return " ".join(identity.lower().split())

    def _confidence(self, recognition: RecognitionResult, comparable_count: int) -> int:
        comp_bonus = min(20, comparable_count * 4)
        base = min(85, max(45, round(recognition.confidence * 0.75)))
        return max(40, min(92, base + comp_bonus))

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
