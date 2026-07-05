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


logger = logging.getLogger("collectiq.pricing.tcgplayer")


class TCGPlayerPricingProvider(PricingProvider):
    provider_name = "tcgplayer"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        api_base: str,
        timeout_seconds: float,
        cache_ttl_seconds: int,
        min_interval_ms: int,
        client=None,
        cache: InMemoryPricingCache | None = None,
        throttle: ProviderThrottle | None = None,
    ) -> None:
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._api_base = api_base.strip().rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._cache = cache or InMemoryPricingCache(cache_ttl_seconds)
        self._throttle = throttle or ProviderThrottle(min_interval_ms)
        self._access_token = ""
        self._token_expires_at = 0.0

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
        product = self._search_product(query, recognition)
        pricing_payload = self._get_product_pricing(product["productId"])
        latency_ms = int((time.perf_counter() - started_at) * 1000)

        result = self._parse_pricing(
            recognition=recognition,
            product=product,
            payload=pricing_payload,
            latency_ms=latency_ms,
        )
        self._cache.set(cache_key, result)
        return result

    def _validate_configuration(self) -> None:
        if not self._client_id or not self._client_secret:
            raise PricingProviderUnavailableError(
                "TCGPLAYER_CLIENT_ID and TCGPLAYER_CLIENT_SECRET are not configured on the backend."
            )
        if not self._is_valid_url(self._api_base):
            raise PricingProviderUnavailableError(
                "TCGPLAYER_API_BASE is missing or invalid."
            )

    def _search_product(self, query: str, recognition: RecognitionResult) -> dict:
        payload = self._request_json(
            "GET",
            self._url("catalog/products"),
            params={
                "productName": query,
                "getExtendedFields": "true",
                "limit": "20",
            },
            authenticated=True,
            retry_on_unauthorized=True,
        )
        products = payload.get("results") or payload.get("data") or []
        if not isinstance(products, list) or not products:
            raise EmptyMarketDataError("TCGplayer returned no product matches.")

        ranked = [
            product
            for product in products
            if isinstance(product, dict) and self._product_id(product)
        ]
        if not ranked:
            raise EmptyMarketDataError("TCGplayer returned no usable product matches.")

        ranked.sort(
            key=lambda product: self._match_score(product, recognition),
            reverse=True,
        )
        product = ranked[0]
        product["productId"] = self._product_id(product)
        return product

    def _get_product_pricing(self, product_id: str) -> dict:
        return self._request_json(
            "GET",
            self._url(f"pricing/product/{product_id}"),
            authenticated=True,
            retry_on_unauthorized=True,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        authenticated: bool = False,
        retry_on_unauthorized: bool = False,
    ) -> dict:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token()}"

        try:
            response = self._send(
                method,
                url,
                headers=headers,
                params=params,
                data=data,
            )
        except httpx.TimeoutException as exc:
            raise PricingProviderTimeoutError(
                "TCGplayer pricing request timed out."
            ) from exc
        except httpx.RequestError as exc:
            raise PricingProviderUnavailableError(
                "TCGplayer pricing request failed before receiving a response."
            ) from exc

        status_code = getattr(response, "status_code", 0)
        if status_code == 401 and authenticated and retry_on_unauthorized:
            self._access_token = ""
            headers["Authorization"] = f"Bearer {self._token(force_refresh=True)}"
            response = self._send(
                method,
                url,
                headers=headers,
                params=params,
                data=data,
            )
            status_code = getattr(response, "status_code", 0)

        if status_code == 404:
            raise EmptyMarketDataError("TCGplayer returned no matching pricing data.")
        if status_code == 429:
            raise PricingProviderRateLimitError("TCGplayer pricing rate limit reached.")
        if status_code >= 500:
            raise PricingProviderUnavailableError(
                f"TCGplayer pricing service returned HTTP {status_code}."
            )
        if status_code >= 400:
            raise PricingProviderError(
                f"TCGplayer pricing request failed with HTTP {status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PricingProviderError(
                "TCGplayer pricing response was not valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise PricingProviderError("TCGplayer pricing response shape was invalid.")
        return payload

    def _token(self, *, force_refresh: bool = False) -> str:
        if (
            not force_refresh
            and self._access_token
            and self._token_expires_at > time.time() + 30
        ):
            return self._access_token

        payload = self._request_token()
        token = str(payload.get("access_token") or payload.get("token") or "").strip()
        if not token:
            raise PricingProviderUnavailableError(
                "TCGplayer OAuth response did not include an access token."
            )

        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        self._access_token = token
        self._token_expires_at = time.time() + max(60, expires_in)
        return token

    def _request_token(self) -> dict:
        try:
            response = self._send(
                "POST",
                self._url("token"),
                headers={"Accept": "application/json"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.TimeoutException as exc:
            raise PricingProviderTimeoutError(
                "TCGplayer OAuth token request timed out."
            ) from exc
        except httpx.RequestError as exc:
            raise PricingProviderUnavailableError(
                "TCGplayer OAuth token request failed before receiving a response."
            ) from exc

        status_code = getattr(response, "status_code", 0)
        if status_code == 429:
            raise PricingProviderRateLimitError("TCGplayer OAuth rate limit reached.")
        if status_code >= 500:
            raise PricingProviderUnavailableError(
                f"TCGplayer OAuth service returned HTTP {status_code}."
            )
        if status_code >= 400:
            raise PricingProviderUnavailableError(
                f"TCGplayer OAuth failed with HTTP {status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PricingProviderError(
                "TCGplayer OAuth response was not valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise PricingProviderError("TCGplayer OAuth response shape was invalid.")
        return payload

    def _send(self, method: str, url: str, **kwargs):
        if self._client is not None:
            if method == "POST":
                return self._client.post(
                    url,
                    timeout=self._timeout_seconds,
                    **kwargs,
                )
            return self._client.get(
                url,
                timeout=self._timeout_seconds,
                **kwargs,
            )

        with httpx.Client(timeout=self._timeout_seconds) as client:
            if method == "POST":
                return client.post(url, **kwargs)
            return client.get(url, **kwargs)

    def _parse_pricing(
        self,
        *,
        recognition: RecognitionResult,
        product: dict,
        payload: dict,
        latency_ms: int,
    ) -> PricingResult:
        pricing_rows = payload.get("results") or payload.get("data") or []
        if not isinstance(pricing_rows, list) or not pricing_rows:
            raise EmptyMarketDataError("TCGplayer returned no pricing rows.")

        comparable_sales: list[MarketComparableSale] = []
        product_name = str(product.get("name") or product.get("cleanName") or recognition.title)
        for row in pricing_rows:
            if not isinstance(row, dict):
                continue
            sale = self._sale_from_pricing_row(row, product_name, recognition)
            if sale is not None:
                comparable_sales.append(sale)

        if not comparable_sales:
            raise EmptyMarketDataError("TCGplayer returned no usable pricing rows.")

        prices = [sale.soldPrice for sale in comparable_sales]
        estimated_value = round(sum(prices) / len(prices))
        confidence = self._confidence(recognition, product, comparable_sales)

        return PricingResult(
            estimatedMarketValue=max(1, estimated_value),
            lowEstimate=max(1, min(prices)),
            highEstimate=max(1, max(prices)),
            currency=comparable_sales[0].currency,
            pricingSource="TCGplayer API",
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
                "sourceConfidence": str(confidence),
                "resultCount": str(len(comparable_sales)),
                "matchedProductId": str(product.get("productId") or ""),
            },
        )

    def _sale_from_pricing_row(
        self,
        row: dict,
        product_name: str,
        recognition: RecognitionResult,
    ) -> MarketComparableSale | None:
        price = self._best_price(row)
        if price is None:
            return None
        condition = str(
            row.get("subTypeName")
            or row.get("condition")
            or row.get("printing")
            or recognition.condition
            or "Unknown"
        )
        title_parts = [
            product_name,
            row.get("printing"),
            row.get("rarity"),
            condition,
        ]
        title = " ".join(
            str(part).strip()
            for part in title_parts
            if isinstance(part, str) and part.strip()
        )
        return MarketComparableSale(
            source="TCGplayer API",
            title=title or product_name,
            soldPrice=price,
            currency=str(row.get("currency") or "USD").upper(),
            soldDate=str(row.get("lastUpdated") or row.get("updatedAt") or utc_timestamp()),
            condition=condition,
            url=row.get("url"),
        )

    def _best_price(self, row: dict) -> int | None:
        for key in (
            "marketPrice",
            "midPrice",
            "directLowPrice",
            "lowPrice",
            "highPrice",
        ):
            try:
                value = row.get(key)
                if value is None:
                    continue
                price = round(float(value))
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
        return None

    def _query_for(self, recognition: RecognitionResult) -> str:
        parts = [
            recognition.title,
            recognition.setName,
            recognition.cardNumber,
            recognition.rarity,
            recognition.brand,
            recognition.year,
        ]
        query = " ".join(
            str(part).strip()
            for part in parts
            if isinstance(part, str) and part.strip()
        )
        return query or recognition.category or "trading card"

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

    def _product_id(self, product: dict) -> str:
        value = product.get("productId") or product.get("id")
        return str(value or "").strip()

    def _match_score(self, product: dict, recognition: RecognitionResult) -> int:
        haystack = " ".join(
            str(value).lower()
            for value in [
                product.get("name"),
                product.get("cleanName"),
                product.get("groupName"),
                product.get("url"),
                self._extended_value(product, "Number"),
                self._extended_value(product, "Rarity"),
            ]
            if value
        )
        score = 0
        for value, weight in [
            (recognition.title, 8),
            (recognition.setName, 5),
            (recognition.cardNumber, 6),
            (recognition.rarity, 3),
            (recognition.brand, 2),
        ]:
            if isinstance(value, str) and value.strip().lower() in haystack:
                score += weight
        return score

    def _extended_value(self, product: dict, name: str) -> str:
        extended = product.get("extendedData") or product.get("extendedFields") or []
        if not isinstance(extended, list):
            return ""
        for item in extended:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").lower() == name.lower():
                return str(item.get("value") or "")
        return ""

    def _confidence(
        self,
        recognition: RecognitionResult,
        product: dict,
        comparable_sales: list[MarketComparableSale],
    ) -> int:
        match_bonus = min(15, self._match_score(product, recognition))
        comp_bonus = min(12, len(comparable_sales) * 3)
        base = min(82, max(45, round(recognition.confidence * 0.72)))
        return max(40, min(94, base + match_bonus + comp_bonus))

    def _trend(self, comparable_sales: list[MarketComparableSale]) -> str:
        if len(comparable_sales) < 3:
            return "Stable"
        prices = [sale.soldPrice for sale in comparable_sales]
        if max(prices) >= min(prices) * 1.2:
            return "Volatile"
        return "Stable"

    def _url(self, path: str) -> str:
        return urljoin(self._api_base, path.lstrip("/"))

    def _is_valid_url(self, value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
