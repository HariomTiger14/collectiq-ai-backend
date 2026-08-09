import re
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
from app.services.pricing.kicksdb_catalog_matcher import KicksDBCatalogMatcher


class KicksDBPricingProvider(PricingProvider):
    provider_name = "kicksdb"

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
        catalog_matcher: KicksDBCatalogMatcher | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._api_base = api_base.strip().rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._cache = cache or InMemoryPricingCache(cache_ttl_seconds)
        self._throttle = throttle or ProviderThrottle(min_interval_ms)
        self._catalog_matcher = catalog_matcher

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

        # Check the locally-backfilled catalog before spending a live
        # KicksDB request -- today, every scan (not just saves) calls this
        # method, so a free catalog hit here directly cuts request volume
        # for anything already backfilled, at the same precision the live
        # search path already returns (see kicksdb_catalog_matcher.py).
        catalog_result = self._catalog_price(recognition, query)
        if catalog_result is not None:
            self._cache.set(cache_key, catalog_result)
            return catalog_result

        self._throttle.acquire(self.provider_name)
        started_at = time.perf_counter()
        payload = self._search_products(recognition, query)
        latency_ms = int((time.perf_counter() - started_at) * 1000)

        products = self._products_from_payload(payload)
        product = self._best_product(products, recognition)
        comparable_sales = self._sales_from_product(product, recognition)
        if not comparable_sales:
            detailed_product = self._detail_product(product)
            if detailed_product:
                product = {**product, **detailed_product}
                comparable_sales = self._sales_from_product(product, recognition)
        if not comparable_sales:
            unified_product = self._unified_product(recognition, product)
            if unified_product:
                product = {**product, **unified_product}
                comparable_sales = self._sales_from_product(product, recognition)
        if not comparable_sales:
            raise EmptyMarketDataError("KicksDB returned no usable sneaker market prices.")

        result = self._pricing_result(
            recognition=recognition,
            query=query,
            product=product,
            comparable_sales=comparable_sales,
            latency_ms=latency_ms,
        )
        self._cache.set(cache_key, result)
        return result

    def _catalog_price(self, recognition: RecognitionResult, query: str) -> PricingResult | None:
        if self._catalog_matcher is None:
            return None
        candidates = self._catalog_matcher.candidates()
        if not candidates:
            return None

        products = [self._catalog_row_to_product(row) for row in candidates]
        try:
            product = self._best_product(products, recognition)
        except EmptyMarketDataError:
            return None

        comparable_sales = self._sales_from_product(product, recognition)
        if not comparable_sales:
            return None

        return self._pricing_result(
            recognition=recognition,
            query=query,
            product=product,
            comparable_sales=comparable_sales,
            latency_ms=0,
            pricing_source="KicksDB Catalog (StockX)",
            pricing_age="cached",
        )

    def _catalog_row_to_product(self, row: dict) -> dict:
        # Reshaped to match the live /v3/stockx/products response shape so
        # the existing _best_product()/_sales_from_product() scoring and
        # sale-extraction logic can be reused unchanged.
        return {
            "id": row.get("kicksdb_id"),
            "title": row.get("title"),
            "brand": row.get("brand"),
            "model": row.get("model"),
            "sku": row.get("sku"),
            "slug": row.get("slug"),
            "link": row.get("product_url"),
            "currency": row.get("currency") or "USD",
            "min_price": _cents_to_dollars(row.get("min_price_cents")),
            "max_price": _cents_to_dollars(row.get("max_price_cents")),
            "avg_price": _cents_to_dollars(row.get("avg_price_cents")),
            "variants": row.get("variants") if isinstance(row.get("variants"), list) else [],
        }

    def _validate_configuration(self) -> None:
        if not self._api_key:
            raise PricingProviderUnavailableError("KICKSDB_API_KEY is not configured.")
        if not self._is_valid_url(self._api_base):
            raise PricingProviderUnavailableError("KICKSDB_API_BASE is missing or invalid.")

    def _request_json(self, url: str, *, params: dict | None = None) -> dict:
        headers = {
            "Accept": "application/json",
            "Authorization": self._auth_header_value(),
        }
        try:
            response = self._send(url, headers=headers, params=params)
        except httpx.TimeoutException as exc:
            raise PricingProviderTimeoutError("KicksDB pricing request timed out.") from exc
        except httpx.RequestError as exc:
            raise PricingProviderUnavailableError(
                "KicksDB pricing request failed before receiving a response."
            ) from exc

        status_code = getattr(response, "status_code", 0)
        if status_code in {401, 403}:
            raise PricingProviderUnavailableError(
                f"KicksDB credentials were rejected with HTTP {status_code}."
            )
        if status_code == 404:
            raise EmptyMarketDataError("KicksDB returned no matching products.")
        if status_code == 429:
            raise PricingProviderRateLimitError("KicksDB pricing rate limit reached.")
        if status_code >= 500:
            raise PricingProviderUnavailableError(
                f"KicksDB pricing service returned HTTP {status_code}."
            )
        if status_code >= 400:
            raise PricingProviderError(
                f"KicksDB pricing request failed with HTTP {status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PricingProviderError("KicksDB pricing response was not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise PricingProviderError("KicksDB pricing response shape was invalid.")
        return payload

    def _send(self, url: str, **kwargs):
        if self._client is not None:
            return self._client.get(url, timeout=self._timeout_seconds, **kwargs)

        with httpx.Client(timeout=self._timeout_seconds) as client:
            return client.get(url, **kwargs)

    def _products_from_payload(self, payload: dict) -> list[dict]:
        for key in ("products", "results", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = self._products_from_payload(value)
                if nested:
                    return nested
        return []

    def _search_products(self, recognition: RecognitionResult, query: str) -> dict:
        url = self._url("v3/stockx/products")
        params = self._search_params(recognition, query)
        payload = self._request_json(url, params=params)
        if self._products_from_payload(payload):
            return payload

        sku = params.get("sku")
        if sku:
            retry_params = {key: value for key, value in params.items() if key != "sku"}
            self._throttle.acquire(self.provider_name)
            retry_payload = self._request_json(url, params=retry_params)
            if self._products_from_payload(retry_payload):
                return retry_payload
        return payload

    def _detail_product(self, product: dict) -> dict | None:
        product_key = self._product_id(product) or self._product_slug(product)
        if not product_key:
            return None

        self._throttle.acquire(self.provider_name)
        payload = self._request_json(
            self._url(f"v3/stockx/products/{product_key}"),
            params={
                "market": "US",
                "display[prices]": "true",
                "display[variants]": "true",
                "display[statistics]": "true",
            },
        )
        if isinstance(payload.get("data"), dict):
            return payload["data"]
        if isinstance(payload.get("product"), dict):
            return payload["product"]
        if all(key not in payload for key in ("products", "results", "items")):
            return payload
        products = self._products_from_payload(payload)
        return products[0] if products else None

    def _unified_product(self, recognition: RecognitionResult, product: dict) -> dict | None:
        params: dict[str, object] = {
            "source": "stockx",
            "limit": 20,
            "sort": "updated_at:desc",
        }
        sku = self._product_sku(product) or recognition.cardNumber
        if sku:
            params["sku"] = sku
        else:
            params["query"] = self._product_title(product) or self._query_for(recognition)

        self._throttle.acquire(self.provider_name)
        payload = self._request_json(self._url("v3/unified/gtin"), params=params)
        products = self._products_from_payload(payload)
        if not products:
            return None
        return self._best_product(products, recognition)

    def _best_product(
        self,
        products: list[dict],
        recognition: RecognitionResult,
    ) -> dict:
        if not products:
            raise EmptyMarketDataError("KicksDB returned no product matches.")

        ranked = sorted(
            products,
            key=lambda product: self._match_score(product, recognition),
            reverse=True,
        )
        product = ranked[0]
        if self._match_score(product, recognition) < 4:
            raise EmptyMarketDataError("KicksDB product match was too weak to value.")
        return product

    def _sales_from_product(
        self,
        product: dict,
        recognition: RecognitionResult,
    ) -> list[MarketComparableSale]:
        title = self._product_title(product) or recognition.title
        currency = self._currency(product)
        sales: list[MarketComparableSale] = []

        for label, price in self._market_prices(product):
            sales.append(
                MarketComparableSale(
                    source="KicksDB StockX",
                    title=f"{title} - {label}",
                    soldPrice=round(price, 2),
                    currency=currency,
                    soldDate=str(
                        product.get("lastUpdated")
                        or product.get("updatedAt")
                        or product.get("releaseDate")
                        or utc_timestamp()
                    ),
                    condition=str(recognition.condition or "Market"),
                    url=self._product_url(product),
                )
            )
        for variant in self._product_variants(product):
            variant_title = self._variant_title(variant)
            for label, price in self._market_prices(variant):
                sales.append(
                    MarketComparableSale(
                        source="KicksDB StockX",
                        title=f"{title} - {variant_title} {label}".strip(),
                        soldPrice=round(price, 2),
                        currency=self._currency(variant, fallback=currency),
                        soldDate=str(
                            variant.get("lastUpdated")
                            or variant.get("updatedAt")
                            or product.get("lastUpdated")
                            or product.get("updatedAt")
                            or utc_timestamp()
                        ),
                        condition=str(recognition.condition or "Market"),
                        url=self._product_url(product),
                    )
                )
        return sales

    def _product_variants(self, product: dict) -> list[dict]:
        variants: list[dict] = []
        for key in ("variants", "sizes", "children"):
            value = product.get(key)
            if isinstance(value, list):
                variants.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                variants.extend(item for item in value.values() if isinstance(item, dict))
        return variants

    def _variant_title(self, variant: dict) -> str:
        value = (
            variant.get("size")
            or variant.get("shoeSize")
            or variant.get("variant")
            or variant.get("name")
            or variant.get("title")
            or "Market"
        )
        text = str(value).strip()
        if text.lower().startswith("size"):
            return text
        return f"Size {text}" if text else "Market"

    def _market_prices(self, product: dict) -> list[tuple[str, float]]:
        prices: list[tuple[str, float]] = []
        for path, label in (
            (("market", "lastSale"), "Last sale"),
            (("market", "averagePrice"), "Average market price"),
            (("market", "avgPrice"), "Average market price"),
            (("market", "marketPrice"), "Market price"),
            (("market", "lowestAsk"), "Lowest ask"),
            (("market", "min_price"), "Lowest market price"),
            (("market", "max_price"), "Highest market price"),
            (("market", "avg_price"), "Average market price"),
            (("market", "lowest_price"), "Lowest market price"),
            (("market", "highest_price"), "Highest market price"),
            (("market", "highestBid"), "Highest bid"),
            (("pricing", "lastSale"), "Last sale"),
            (("pricing", "averagePrice"), "Average market price"),
            (("pricing", "marketPrice"), "Market price"),
            (("pricing", "lowestAsk"), "Lowest ask"),
            (("pricing", "min_price"), "Lowest market price"),
            (("pricing", "max_price"), "Highest market price"),
            (("pricing", "avg_price"), "Average market price"),
            (("lastSale",), "Last sale"),
            (("averagePrice",), "Average market price"),
            (("avgPrice",), "Average market price"),
            (("avg_price",), "Average market price"),
            (("marketPrice",), "Market price"),
            (("price",), "Market price"),
            (("min_price",), "Lowest market price"),
            (("max_price",), "Highest market price"),
            (("lowest_price",), "Lowest market price"),
            (("highest_price",), "Highest market price"),
            (("lowestAsk",), "Lowest ask"),
            (("highestBid",), "Highest bid"),
            (("prices", "lastSale"), "Last sale"),
            (("prices", "last_sale"), "Last sale"),
            (("prices", "averagePrice"), "Average market price"),
            (("prices", "average_price"), "Average market price"),
            (("prices", "avgPrice"), "Average market price"),
            (("prices", "marketPrice"), "Market price"),
            (("prices", "market_price"), "Market price"),
            (("prices", "lowestAsk"), "Lowest ask"),
            (("prices", "lowest_ask"), "Lowest ask"),
            (("prices", "highestBid"), "Highest bid"),
            (("prices", "highest_bid"), "Highest bid"),
            (("statistics", "lastSale"), "Last sale"),
            (("statistics", "averagePrice"), "Average market price"),
            (("statistics", "avg_price"), "Average market price"),
            (("statistics", "min_price"), "Lowest market price"),
            (("statistics", "max_price"), "Highest market price"),
            (("statistics", "lowestAsk"), "Lowest ask"),
            (("statistics", "highestBid"), "Highest bid"),
        ):
            price = self._price_at_path(product, path)
            if price is not None:
                prices.append((label, price))

        prices.extend(self._prices_from_map(product.get("prices")))

        deduped: list[tuple[str, float]] = []
        seen: set[tuple[str, int]] = set()
        for label, price in prices:
            key = (label, round(price * 100))
            if key in seen:
                continue
            seen.add(key)
            deduped.append((label, price))
        return deduped

    def _prices_from_map(self, value: object) -> list[tuple[str, float]]:
        if not isinstance(value, dict):
            return []

        results: list[tuple[str, float]] = []
        for key, nested_value in value.items():
            label = self._price_label(str(key))
            price = self._parse_price(nested_value)
            if price is not None and label:
                results.append((label, price))
                continue
            if isinstance(nested_value, dict):
                nested_price = self._parse_price(
                    nested_value.get("value")
                    or nested_value.get("amount")
                    or nested_value.get("price")
                    or nested_value.get("lastSale")
                    or nested_value.get("lowestAsk")
                    or nested_value.get("highestBid")
                )
                if nested_price is not None and label:
                    results.append((label, nested_price))
        return results

    def _price_label(self, key: str) -> str:
        normalized = key.lower().replace("-", "_").replace(" ", "_")
        if normalized in {"retail", "retail_price", "msrp"}:
            return ""
        if normalized in {"min_price", "lowest_price"}:
            return "Lowest market price"
        if normalized in {"max_price", "highest_price"}:
            return "Highest market price"
        if normalized in {"avg_price", "average_price"}:
            return "Average market price"
        if "last" in normalized and "sale" in normalized:
            return "Last sale"
        if "lowest" in normalized and "ask" in normalized:
            return "Lowest ask"
        if "highest" in normalized and "bid" in normalized:
            return "Highest bid"
        if "average" in normalized or normalized.startswith("avg"):
            return "Average market price"
        if "market" in normalized:
            return "Market price"
        if "price" in normalized:
            return key.replace("_", " ").replace("-", " ").title()
        return key.replace("_", " ").replace("-", " ").title()

    def _price_at_path(self, payload: dict, path: tuple[str, ...]) -> float | None:
        value: object = payload
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return self._parse_price(value)

    def _parse_price(self, value: object) -> float | None:
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        if isinstance(value, str):
            cleaned = re.sub(r"[^0-9.]", "", value)
            try:
                price = float(cleaned)
            except ValueError:
                return None
            return price if price > 0 else None
        return None

    def _pricing_result(
        self,
        *,
        recognition: RecognitionResult,
        query: str,
        product: dict,
        comparable_sales: list[MarketComparableSale],
        latency_ms: int,
        pricing_source: str = "KicksDB StockX API",
        pricing_age: str = "live",
    ) -> PricingResult:
        prices = [sale.soldPrice for sale in comparable_sales]
        estimated_value = round(sum(prices) / len(prices))
        confidence = self._confidence(recognition, product, comparable_sales)

        return PricingResult(
            estimatedMarketValue=max(1, estimated_value),
            lowEstimate=max(1, min(prices)),
            highEstimate=max(1, max(prices)),
            currency=comparable_sales[0].currency,
            pricingSource=pricing_source,
            pricingConfidence=confidence,
            lastUpdated=utc_timestamp(),
            marketTrend=self._trend(comparable_sales),
            sourceCount=1,
            pricingAge=pricing_age,
            comparableSales=comparable_sales,
            cacheStatus="miss",
            providerDiagnostics={
                "provider": self.provider_name,
                "cacheStatus": "miss",
                "responseLatencyMs": str(latency_ms),
                "pricingFreshness": pricing_age,
                "fallbackReason": "",
                "sourceConfidence": str(confidence),
                "resultCount": str(len(comparable_sales)),
                "matchedProductId": self._product_id(product),
                "matchedProductName": self._product_title(product),
                "matchedProductSku": self._product_sku(product),
                "query": query,
                "marketplace": "StockX",
            },
        )

    def _query_for(self, recognition: RecognitionResult) -> str:
        parts = [
            recognition.brand,
            recognition.title,
            recognition.series,
            recognition.cardNumber,
            recognition.year,
        ]
        query = " ".join(
            str(part).strip()
            for part in parts
            if isinstance(part, str) and part.strip()
        )
        return query or recognition.category or "sneakers"

    def _search_params(self, recognition: RecognitionResult, query: str) -> dict[str, str]:
        params = {
            "query": query,
            "market": "US",
            "display[prices]": "true",
            "display[variants]": "true",
            "display[statistics]": "true",
        }
        sku = self._normalized_sku(recognition.cardNumber)
        if sku:
            params["sku"] = sku
        return params

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
        haystack = self._product_text(product)
        score = 0
        for value, weight in [
            (recognition.title, 6),
            (recognition.brand, 5),
            (recognition.series, 3),
            (recognition.cardNumber, 6),
            (recognition.year, 2),
        ]:
            if isinstance(value, str) and value.strip().lower() in haystack:
                score += weight
        if any(term in haystack for term in ("sneaker", "shoe", "streetwear", "stockx")):
            score += 2
        return score

    def _confidence(
        self,
        recognition: RecognitionResult,
        product: dict,
        comparable_sales: list[MarketComparableSale],
    ) -> int:
        match_bonus = min(16, self._match_score(product, recognition) * 2)
        comp_bonus = min(8, len(comparable_sales) * 2)
        base = min(76, max(45, round(recognition.confidence * 0.68)))
        return max(40, min(90, base + match_bonus + comp_bonus))

    def _trend(self, comparable_sales: list[MarketComparableSale]) -> str:
        prices = [sale.soldPrice for sale in comparable_sales]
        if len(prices) < 2:
            return "Stable"
        if max(prices) >= min(prices) * 1.25:
            return "Volatile"
        return "Stable"

    def _product_text(self, product: dict) -> str:
        values = [
            self._product_title(product),
            product.get("brand"),
            product.get("make"),
            product.get("model"),
            product.get("name"),
            product.get("title"),
            product.get("subtitle"),
            product.get("category"),
            product.get("gender"),
            product.get("colorway"),
            self._product_sku(product),
            self._product_url(product),
        ]
        return " ".join(str(value).lower() for value in values if value)

    def _product_title(self, product: dict) -> str:
        return str(
            product.get("title")
            or product.get("name")
            or product.get("productName")
            or product.get("shoeName")
            or ""
        ).strip()

    def _product_sku(self, product: dict) -> str:
        return str(
            product.get("styleId")
            or product.get("style_id")
            or product.get("sku")
            or product.get("modelNumber")
            or product.get("variant_id")
            or ""
        ).strip()

    def _normalized_sku(self, value: object) -> str:
        if not isinstance(value, str):
            return ""
        text = value.strip()
        return text if re.search(r"[A-Za-z]", text) and re.search(r"\d", text) else ""

    def _product_id(self, product: dict) -> str:
        return str(
            product.get("id")
            or product.get("uuid")
            or product.get("product_id")
            or product.get("source_product_id")
            or product.get("slug")
            or ""
        ).strip()

    def _product_slug(self, product: dict) -> str:
        return str(product.get("slug") or product.get("urlKey") or product.get("url_key") or "").strip()

    def _product_url(self, product: dict) -> str | None:
        url = product.get("url") or product.get("productUrl") or product.get("stockxUrl") or product.get("link")
        return str(url).strip() if url else None

    def _currency(self, product: dict, *, fallback: str = "USD") -> str:
        market = product.get("market")
        pricing = product.get("pricing")
        currency = (
            product.get("currency")
            or (market.get("currency") if isinstance(market, dict) else None)
            or (pricing.get("currency") if isinstance(pricing, dict) else None)
            or fallback
        )
        return str(currency).upper()

    def _auth_header_value(self) -> str:
        if self._api_key.lower().startswith("bearer "):
            return self._api_key
        return self._api_key

    def _url(self, path: str) -> str:
        return urljoin(self._api_base, path.lstrip("/"))

    def _is_valid_url(self, value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _cents_to_dollars(value: object) -> float | None:
    if isinstance(value, int) and value > 0:
        return value / 100
    return None
