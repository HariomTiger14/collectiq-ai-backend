import logging
import time
from dataclasses import replace
from urllib.parse import urljoin, urlparse

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


logger = logging.getLogger("collectiq.pricing.pricecharting")


class PriceChartingPricingProvider(PricingProvider):
    provider_name = "pricecharting"

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str,
        timeout_seconds: float,
        cache_ttl_seconds: int,
        min_interval_ms: int,
        client=None,
        cache: InMemoryPricingCache | None = None,
        throttle: ProviderThrottle | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._api_base = api_base.strip().rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._cache = cache or InMemoryPricingCache(cache_ttl_seconds)
        self._throttle = throttle or ProviderThrottle(min_interval_ms)

    def price(self, recognition: RecognitionResult) -> PricingResult:
        self._validate_configuration()

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
        payload = self._request(query)
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        result = self._parse_response(
            recognition=recognition,
            payload=payload,
            latency_ms=latency_ms,
        )
        self._cache.set(cache_key, result)
        return result

    def _validate_configuration(self) -> None:
        if not self._api_key:
            raise PricingProviderUnavailableError(
                "PRICECHARTING_API_KEY is not configured on the backend."
            )
        if not self._is_valid_url(self._api_base):
            raise PricingProviderUnavailableError(
                "PRICECHARTING_API_BASE is missing or invalid."
            )

    def _request(self, query: str) -> dict:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        params = {
            "q": query,
            "t": self._api_key,
            "format": "json",
        }

        try:
            response = self._send(
                self._url("api/products"),
                headers=headers,
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise PricingProviderTimeoutError(
                "PriceCharting pricing request timed out."
            ) from exc
        except httpx.RequestError as exc:
            raise PricingProviderUnavailableError(
                "PriceCharting pricing request failed before receiving a response."
            ) from exc

        status_code = getattr(response, "status_code", 0)
        if status_code in {401, 403}:
            raise PricingProviderUnavailableError(
                f"PriceCharting authorization failed with HTTP {status_code}."
            )
        if status_code == 404:
            raise EmptyMarketDataError("PriceCharting returned no matching product.")
        if status_code == 429:
            raise PricingProviderRateLimitError(
                "PriceCharting pricing rate limit reached."
            )
        if status_code >= 500:
            raise PricingProviderUnavailableError(
                f"PriceCharting pricing service returned HTTP {status_code}."
            )
        if status_code >= 400:
            raise PricingProviderError(
                f"PriceCharting pricing request failed with HTTP {status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PricingProviderError(
                "PriceCharting pricing response was not valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise PricingProviderError("PriceCharting pricing response shape was invalid.")
        return payload

    def _send(self, url: str, **kwargs):
        if self._client is not None:
            return self._client.get(
                url,
                timeout=self._timeout_seconds,
                **kwargs,
            )

        with httpx.Client(timeout=self._timeout_seconds) as client:
            return client.get(url, **kwargs)

    def _parse_response(
        self,
        *,
        recognition: RecognitionResult,
        payload: dict,
        latency_ms: int,
    ) -> PricingResult:
        products = self._products_from_payload(payload)
        if not products:
            raise EmptyMarketDataError("PriceCharting returned no pricing results.")

        products.sort(
            key=lambda product: self._match_score(product, recognition),
            reverse=True,
        )
        product = products[0]
        comparable_sales = self._sales_from_product(product, recognition)
        if not comparable_sales:
            raise EmptyMarketDataError("PriceCharting returned no usable price fields.")

        prices = [sale.soldPrice for sale in comparable_sales]
        confidence = self._confidence(recognition, product, comparable_sales)

        return PricingResult(
            estimatedMarketValue=max(1, round(sum(prices) / len(prices))),
            lowEstimate=max(1, min(prices)),
            highEstimate=max(1, max(prices)),
            currency=comparable_sales[0].currency,
            pricingSource="PriceCharting API",
            pricingConfidence=confidence,
            lastUpdated=utc_timestamp(),
            marketTrend="Stable",
            sourceCount=1,
            pricingAge="guide",
            comparableSales=comparable_sales,
            cacheStatus="miss",
            providerDiagnostics={
                "provider": self.provider_name,
                "cacheStatus": "miss",
                "responseLatencyMs": str(latency_ms),
                "pricingFreshness": "guide",
                "fallbackReason": "",
                "sourceConfidence": str(confidence),
                "resultCount": str(len(comparable_sales)),
                "matchedProductId": str(
                    product.get("id")
                    or product.get("product-id")
                    or product.get("productId")
                    or ""
                ),
            },
        )

    def _products_from_payload(self, payload: dict) -> list[dict]:
        candidates = (
            payload.get("products")
            or payload.get("results")
            or payload.get("data")
            or payload.get("items")
        )
        if isinstance(candidates, list):
            return [item for item in candidates if isinstance(item, dict)]
        if isinstance(candidates, dict):
            return [candidates]
        product_keys = {
            "product-name",
            "productName",
            "console-name",
            "loose-price",
            "cib-price",
            "new-price",
            "graded-price",
        }
        if any(key in payload for key in product_keys):
            return [payload]
        return []

    def _sales_from_product(
        self,
        product: dict,
        recognition: RecognitionResult,
    ) -> list[MarketComparableSale]:
        explicit_sales = product.get("sales") or product.get("recentSales")
        if isinstance(explicit_sales, list):
            sales: list[MarketComparableSale] = []
            for row in explicit_sales:
                if not isinstance(row, dict):
                    continue
                sale = self._sale_from_recent_row(row, product, recognition)
                if sale is not None:
                    sales.append(sale)
            if sales:
                return sales

        product_name = self._product_name(product, recognition)
        price_fields = [
            ("loose-price", "Loose"),
            ("loosePrice", "Loose"),
            ("complete-price", "Complete"),
            ("cib-price", "Complete in Box"),
            ("cibPrice", "Complete in Box"),
            ("new-price", "New"),
            ("newPrice", "New"),
            ("graded-price", "Graded"),
            ("gradedPrice", "Graded"),
            ("box-only-price", "Box Only"),
            ("manual-only-price", "Manual Only"),
        ]
        sales: list[MarketComparableSale] = []
        for key, condition in price_fields:
            price = self._parse_price(product.get(key))
            if price is None:
                continue
            sales.append(
                MarketComparableSale(
                    source="PriceCharting API",
                    title=f"{product_name} {condition}",
                    soldPrice=price,
                    currency=self._currency(product),
                    soldDate=str(
                        product.get("lastUpdated")
                        or product.get("updatedAt")
                        or product.get("date")
                        or utc_timestamp()
                    ),
                    condition=condition,
                    url=product.get("url") or product.get("product-url"),
                )
            )
        return sales

    def _sale_from_recent_row(
        self,
        row: dict,
        product: dict,
        recognition: RecognitionResult,
    ) -> MarketComparableSale | None:
        price = self._parse_price(
            row.get("price")
            or row.get("soldPrice")
            or row.get("sale-price")
            or row.get("value")
        )
        if price is None:
            return None
        condition = str(
            row.get("condition")
            or row.get("type")
            or recognition.condition
            or "Unknown"
        )
        return MarketComparableSale(
            source="PriceCharting API",
            title=str(row.get("title") or self._product_name(product, recognition)),
            soldPrice=price,
            currency=str(row.get("currency") or self._currency(product)).upper(),
            soldDate=str(
                row.get("date")
                or row.get("soldDate")
                or row.get("sold-date")
                or utc_timestamp()
            ),
            condition=condition,
            url=row.get("url"),
        )

    def _parse_price(self, value) -> int | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").strip()
        try:
            price = round(float(value))
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    def _query_for(self, recognition: RecognitionResult) -> str:
        parts = [
            recognition.title,
            recognition.setName,
            recognition.cardNumber,
            recognition.brand,
            recognition.year,
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

    def _match_score(self, product: dict, recognition: RecognitionResult) -> int:
        haystack = " ".join(
            str(value).lower()
            for value in [
                product.get("product-name"),
                product.get("productName"),
                product.get("name"),
                product.get("console-name"),
                product.get("set"),
                product.get("series"),
            ]
            if value
        )
        score = 0
        for value, weight in [
            (recognition.title, 8),
            (recognition.setName, 5),
            (recognition.cardNumber, 6),
            (recognition.brand, 3),
            (recognition.year, 2),
        ]:
            if isinstance(value, str) and value.strip().lower() in haystack:
                score += weight
        return score

    def _confidence(
        self,
        recognition: RecognitionResult,
        product: dict,
        comparable_sales: list[MarketComparableSale],
    ) -> int:
        match_bonus = min(14, self._match_score(product, recognition))
        comp_bonus = min(10, len(comparable_sales) * 2)
        base = min(78, max(42, round(recognition.confidence * 0.68)))
        return max(35, min(90, base + match_bonus + comp_bonus))

    def _product_name(self, product: dict, recognition: RecognitionResult) -> str:
        return str(
            product.get("product-name")
            or product.get("productName")
            or product.get("name")
            or recognition.title
        )

    def _currency(self, product: dict) -> str:
        return str(product.get("currency") or "USD").upper()

    def _url(self, path: str) -> str:
        return urljoin(self._api_base, path.lstrip("/"))

    def _is_valid_url(self, value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
