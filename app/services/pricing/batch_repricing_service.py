"""Scheduled batch re-pricing.

Iterates every ``portfolio_items`` row, re-values it through the same pricing
engine the on-demand ``/reprice`` endpoint uses, and persists refreshed values
back to the row so the app (and the server-side alert evaluator) act on fresh
numbers. This is the "make pricing live" half of the pipeline that pairs with
``PriceAlertEvaluationService`` — re-price first, then alerts fire on the new
values.

Persistence mirrors the mobile write path (``supabaseRowForItem``): it updates
the ``estimated_value_low`` / ``estimated_value_high`` columns *and* the
``raw_json`` valuation fields. The app's read path
(``_valuationOverridesFromSupabaseRow``) heals a trusted ``market_estimated``
value from those columns, so both planes stay consistent.

Safety: a re-price that comes back *unavailable* (e.g. ``PRICING_PROVIDER=mock``
on SIT, or no market match) is a **no-op** — it never zeroes out an existing
value. Only a real ``market_estimated`` result with a positive value is written.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from app.core.config import settings
from app.schemas.pricing import (
    RepriceIdentityRequest,
    RepricePricingResponse,
    RepriceRequest,
)
from app.services.pricing.base_pricing_provider import (
    PricingProviderRateLimitError,
    PricingProviderTimeoutError,
)
from app.services.pricing.catalog_search_service import (
    CatalogItemNotFoundError,
    CatalogSearchError,
    CatalogSearchService,
)
from app.services.pricing.currency_conversion import _exchange_rate
from app.services.pricing.reprice_service import (
    RepriceService,
    RepriceValidationError,
    _display_string,
)


@dataclass
class RepricingSummary:
    scanned: int = 0
    repriced: int = 0
    unavailable: int = 0
    skipped: int = 0
    rate_limited: int = 0
    dry_run: bool = False
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "repriced": self.repriced,
            "unavailable": self.unavailable,
            "skipped": self.skipped,
            "rateLimited": self.rate_limited,
            "dryRun": self.dry_run,
            "errors": self.errors,
        }


class BatchRepricingService:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        client: httpx.Client | None = None,
        reprice_service: RepriceService | None = None,
        catalog_search: CatalogSearchService | None = None,
        max_rate_limit_retries: int = 3,
        retry_backoff_seconds: float = 1.1,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).rstrip("/")
        self._service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        )
        self._client = client or httpx.Client(timeout=30.0)
        self._reprice = reprice_service or RepriceService()
        # Items linked to a pricecharting_catalog row (see
        # portfolio_catalog_matching_service.py) get priced from the catalog
        # directly instead of a live provider call -- same data, no external
        # API hit. Unmatched items keep the live-API path below unchanged.
        self._catalog_search = catalog_search or CatalogSearchService(
            supabase_url=self._supabase_url,
            service_role_key=self._service_role_key,
        )
        # The provider throttle REJECTS (raises) rather than waiting when calls
        # arrive faster than its min interval (PriceCharting: 1s, shared across
        # processes). Sweeping many same-provider items back-to-back therefore
        # loses some to rate-limit errors. Retry those with a short backoff —
        # slightly above the strictest interval — instead of dropping them.
        self._max_rate_limit_retries = max(0, max_rate_limit_retries)
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._sleep = sleep

    def reprice_all(
        self,
        *,
        limit: int = 1000,
        page_size: int = 200,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> RepricingSummary:
        current_time = now or datetime.now(timezone.utc)
        summary = RepricingSummary(dry_run=dry_run)
        offset = 0
        while summary.scanned < limit:
            page = self._fetch_items_page(
                offset=offset,
                page_size=min(page_size, limit - summary.scanned),
            )
            if not page:
                break
            for row in page:
                summary.scanned += 1
                self._reprice_row(row, summary, current_time, dry_run)
            if len(page) < page_size:
                break
            offset += len(page)
        return summary

    # --- Per-row -------------------------------------------------------------

    def _reprice_row(
        self,
        row: dict[str, Any],
        summary: RepricingSummary,
        now: datetime,
        dry_run: bool,
    ) -> None:
        catalog_id = row.get("pricecharting_id")
        if catalog_id:
            pricing = self._price_from_catalog(catalog_id, row, summary)
            if pricing is None:
                return
        else:
            request = _request_from_row(row)
            if request is None:
                summary.skipped += 1
                return
            response = self._reprice_with_retry(request, row, summary)
            if response is None:
                return
            pricing = response.pricing

        value = pricing.estimatedMarketValue
        if pricing.status != "available" or value is None or value <= 0:
            summary.unavailable += 1
            return

        summary.repriced += 1
        if not dry_run:
            try:
                self._persist(row, pricing, now)
            except Exception as error:  # noqa: BLE001
                summary.repriced -= 1
                summary.errors.append({"id": row.get("id"), "error": str(error)})

    def _price_from_catalog(
        self,
        catalog_id: str,
        row: dict[str, Any],
        summary: RepricingSummary,
    ) -> RepricePricingResponse | None:
        """Reads a price straight from pricecharting_catalog for an item
        already linked by portfolio_catalog_matching_service, instead of
        calling a live pricing provider. Returns None (counters already
        updated) when the catalog row is gone or the request itself fails;
        an unpriced/zero catalog entry still returns a response with
        status="unavailable" so it's handled the same safe no-op way as a
        live-API "unavailable" result (never zeroes an existing value)."""
        try:
            detail = self._catalog_search.detail(catalog_id)
        except CatalogItemNotFoundError:
            summary.unavailable += 1
            return None
        except CatalogSearchError as error:
            summary.errors.append({"id": row.get("id"), "error": str(error)})
            return None

        result = detail.result
        pricing = result.pricing
        available = pricing.marketValue is not None and pricing.marketValue > 0

        # The catalog stores native provider currency (e.g. USD); the live-API
        # path always converts to the item's own display currency before
        # persisting (RepriceService -> convert_pricing_result()) -- this must
        # match, or a catalog-matched item's value gets stored as if its raw
        # USD amount were AUD (understating it by the FX rate, ~35% for USD).
        display_currency = _display_currency_from_row(row)
        rate = _exchange_rate(pricing.currency, display_currency)
        value = _to_float(pricing.marketValue * rate) if available else None
        low = _to_float(pricing.lowEstimate * rate) if pricing.lowEstimate else None
        high = _to_float(pricing.highEstimate * rate) if pricing.highEstimate else None

        return RepricePricingResponse(
            status="available" if available else "unavailable",
            estimatedMarketValue=value,
            lowEstimate=low,
            highEstimate=high,
            currency=display_currency,
            displayString=_display_string(value, display_currency) if available else None,
            confidenceScore=result.confidence,
            pricingConfidence=round(result.confidence * 100),
            valuationStrategy="catalog_lookup",
            pricingSource={"name": result.source, "kind": "catalog"},
        )

    def _reprice_with_retry(
        self,
        request: RepriceRequest,
        row: dict[str, Any],
        summary: RepricingSummary,
    ):
        """Price one item, retrying on throttle rejection (rate-limit/timeout)
        with a backoff. Returns the RepriceResponse, or None if the row was
        skipped or errored (counters already updated)."""
        attempt = 0
        while True:
            try:
                return self._reprice.reprice(request)
            except RepriceValidationError:
                summary.skipped += 1
                return None
            except (
                PricingProviderRateLimitError,
                PricingProviderTimeoutError,
            ) as error:
                if attempt < self._max_rate_limit_retries:
                    attempt += 1
                    summary.rate_limited += 1
                    if self._retry_backoff_seconds > 0:
                        self._sleep(self._retry_backoff_seconds)
                    continue
                summary.errors.append({"id": row.get("id"), "error": str(error)})
                return None
            except Exception as error:  # noqa: BLE001 - one bad row can't stop the sweep
                summary.errors.append({"id": row.get("id"), "error": str(error)})
                return None

    def _persist(self, row: dict[str, Any], pricing: Any, now: datetime) -> None:
        iso = now.isoformat()
        value = float(pricing.estimatedMarketValue)
        low = _to_float(pricing.lowEstimate)
        high = _to_float(pricing.highEstimate)
        currency = (pricing.currency or "AUD").strip().upper() or "AUD"
        source = _source_name(pricing) or "market"

        raw = dict(row.get("raw_json") or {})
        raw_pricing = dict(raw.get("pricing") or {})
        raw_pricing.update(
            {
                "estimatedMarketValue": value,
                "lowEstimate": low if low is not None else value,
                "highEstimate": high if high is not None else value,
                "currency": currency,
                "pricingSource": source,
                "pricingConfidence": pricing.pricingConfidence,
                "lastUpdated": iso,
                "valuationStatus": "market_estimated",
                "valuationSource": source,
            }
        )
        # Everything below is *derived from the price we just replaced*, so
        # leaving it behind produces a self-contradicting record. That is not
        # hypothetical: a Pokemon card whose original scan matched a video
        # game kept displayString "$62.00 AUD" and a 56/149 AUD range next to
        # a freshly repriced estimatedMarketValue of 1.84, and the app renders
        # displayString verbatim -- so the item still *read* as $62 long after
        # it had been correctly repriced. Recompute what we can and drop what
        # no longer has a basis, rather than letting the old match linger.
        raw_pricing["displayString"] = (
            _clean(getattr(pricing, "displayString", None))
            or _display_string(value, currency)
        )
        original = dict(getattr(pricing, "originalMarketPayload", None) or {})
        original_value = _to_float(original.get("price"))
        original_currency = _clean(original.get("currency"))
        if original_value is not None and original_value > 0 and original_currency:
            raw_pricing["originalPrice"] = original_value
            raw_pricing["originalCurrency"] = original_currency.upper()
            raw_pricing["exchangeRateUsed"] = _to_float(
                original.get("exchangeRateUsed")
            )
            raw_pricing["exchangeRateDate"] = (
                _clean(original.get("exchangeRateDate")) or iso
            )
        else:
            for stale_key in (
                "originalPrice",
                "originalCurrency",
                "exchangeRateUsed",
                "exchangeRateDate",
            ):
                raw_pricing.pop(stale_key, None)
        # lowEstimateAud/highEstimateAud are a denormalised copy of the range
        # in a currency this job does not recompute; a stale pair is worse
        # than an absent one, since consumers cannot tell it is out of date.
        raw_pricing.pop("lowEstimateAud", None)
        raw_pricing.pop("highEstimateAud", None)
        match_metadata = dict(getattr(pricing, "matchMetadata", None) or {})
        diagnostics = dict(getattr(pricing, "diagnostics", None) or {})
        explanation = _clean(
            match_metadata.get("reason") or diagnostics.get("priceExplanation")
        )
        if explanation:
            raw_pricing["pricingExplanation"] = explanation
        else:
            raw_pricing.pop("pricingExplanation", None)
        raw_pricing.pop("reasonCode", None)

        # The comparable sales are the clearest evidence of *which product*
        # was priced. Keeping the previous match's comps (e.g. "Raichu #19
        # Box Only") alongside a new price makes the mismatch invisible.
        market = _market_summary_from(pricing, value, low, high, iso)
        raw.update(
            {
                "estimatedValue": value,
                "valuationStatus": "market_estimated",
                "valuationSource": source,
                "pricing": raw_pricing,
                "marketSummary": market,
                "lastValueRefreshedAt": iso,
            }
        )

        self._supabase_request(
            "PATCH",
            "/rest/v1/portfolio_items",
            params={
                "id": f"eq.{row['id']}",
                "user_id": f"eq.{row['user_id']}",
            },
            headers={"Prefer": "return=minimal"},
            json={
                "raw_json": raw,
                "estimated_value_low": low if low is not None else value,
                "estimated_value_high": high if high is not None else value,
                "updated_at": iso,
            },
        )
        self._persist_valuation_snapshot(row, pricing, value, low, high, iso, source)

    def _persist_valuation_snapshot(
        self,
        row: dict[str, Any],
        pricing: Any,
        value: float,
        low: float | None,
        high: float | None,
        iso: str,
        source: str,
    ) -> None:
        """Writes a permanent daily price point for this item, mirroring the
        mobile app's own cloud row shape (``supabaseValuationSnapshotRowForItem``)
        so rows written by the scheduled sweep and rows written by a user's
        manual "refresh valuation" tap are indistinguishable in storage. This
        is what powers the value-history chart with real daily data instead
        of only updating on a manual refresh.
        """
        raw = row.get("raw_json") or {}
        try:
            self._supabase_request(
                "POST",
                "/rest/v1/portfolio_valuation_snapshots",
                headers={"Prefer": "return=minimal"},
                json={
                    "user_id": row["user_id"],
                    "portfolio_item_id": row["id"],
                    "value_aud": value,
                    "low_estimate_aud": low if low is not None else value,
                    "high_estimate_aud": high if high is not None else value,
                    "currency": pricing.currency,
                    "display_string": pricing.displayString,
                    "valuation_status": "market_estimated",
                    "reason_code": "market_estimated",
                    "valuation_strategy": pricing.valuationStrategy,
                    "pricing_provider": source,
                    "attribution_text": source,
                    "confidence_score": pricing.confidenceScore,
                    "condition_label": _clean(raw.get("condition")),
                    "priced_at": iso,
                    "evidence_json": {
                        "title": _clean(raw.get("title")) or _clean(row.get("title")),
                        "category": _clean(raw.get("category"))
                        or _clean(row.get("category")),
                        "brand": _clean(raw.get("brand"))
                        or _clean(row.get("manufacturer")),
                        "condition": _clean(raw.get("condition")),
                        "source": "scheduled_reprice",
                    },
                },
            )
        except Exception:  # noqa: BLE001 - a missed history point can't block the sweep
            pass

    # --- Supabase access -----------------------------------------------------

    def _fetch_items_page(
        self, *, offset: int, page_size: int
    ) -> list[dict[str, Any]]:
        response = self._supabase_request(
            "GET",
            "/rest/v1/portfolio_items",
            params={
                "select": "id,user_id,category,title,manufacturer,series,year,"
                "raw_json,estimated_value_high,estimated_value_low,pricecharting_id",
                "order": "id.asc",
                "limit": str(page_size),
                "offset": str(offset),
            },
        )
        data = response.json()
        return data if isinstance(data, list) else []

    def _supabase_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        response = self._client.request(
            method,
            f"{self._supabase_url}{path}",
            params=params,
            headers={
                "apikey": self._service_role_key,
                "Authorization": f"Bearer {self._service_role_key}",
                "Content-Type": "application/json",
                **(headers or {}),
            },
            json=json,
        )
        response.raise_for_status()
        return response


def _request_from_row(row: dict[str, Any]) -> RepriceRequest | None:
    raw = row.get("raw_json") or {}
    title = _clean(raw.get("title")) or _clean(row.get("title"))
    category = _clean(raw.get("category")) or _clean(row.get("category"))
    if not title or len(title) < 2 or not category or len(category) < 2:
        return None
    currency = _display_currency_from_row(row)
    identity = RepriceIdentityRequest(
        title=title,
        category=category,
        brand=_clean(raw.get("brand")) or _clean(row.get("manufacturer")),
        setName=_clean(raw.get("setName")),
        series=_clean(raw.get("series")) or _clean(row.get("series")),
        cardNumber=_clean(raw.get("cardNumber")),
        sku=_clean(raw.get("sku")),
        upc=_clean(raw.get("upc")),
        condition=_clean(raw.get("condition")),
        year=_clean(raw.get("year")) or _clean(_year_text(row.get("year"))),
        edition=_clean(raw.get("edition")),
        language=_clean(raw.get("language")),
        rarity=_clean(raw.get("rarity")),
        playerOrCharacter=_clean(raw.get("playerOrCharacter")),
        estimatedGrade=_clean(raw.get("estimatedGrade")),
        notes=_clean(raw.get("notes")),
    )
    return RepriceRequest(
        itemId=row.get("id"),
        previousValue=_to_float(raw.get("estimatedValue")),
        previousCurrency=currency,
        displayCurrency=currency,
        correctionSource="scheduled_reprice",
        identity=identity,
    )


def _market_summary_from(
    pricing: Any,
    value: float,
    low: float | None,
    high: float | None,
    iso: str,
) -> dict[str, Any]:
    """Rebuild marketSummary from the pricing we just computed.

    Merging into the previous summary would keep the old match's comparable
    sales, which are the clearest evidence of *which product* was priced --
    a card left showing "Raichu #19 Box Only" comps next to a corrected card
    price hides the fact that the two came from different products.
    """
    comps = list(getattr(pricing, "comparableSales", None) or [])
    prices = [
        _to_float(getattr(comp, "soldPrice", None))
        for comp in comps
    ]
    prices = [price for price in prices if price is not None and price > 0]
    prices.sort()
    average = sum(prices) / len(prices) if prices else value
    if prices:
        middle = len(prices) // 2
        median = (
            prices[middle]
            if len(prices) % 2
            else (prices[middle - 1] + prices[middle]) / 2
        )
    else:
        median = value
    return {
        "averagePrice": average,
        "medianPrice": median,
        "lowPrice": low if low is not None else value,
        "highPrice": high if high is not None else value,
        "salesCount": len(comps),
        "trendLabel": getattr(pricing, "marketTrend", None) or "Stable",
        "confidence": _to_float(getattr(pricing, "pricingConfidence", None)),
        "lastUpdated": iso,
        "sources": [source] if (source := _source_name(pricing)) else [],
        "comps": [
            {
                "source": comp.source,
                "title": comp.title,
                "soldPrice": _to_float(comp.soldPrice),
                "currency": comp.currency,
                "soldDate": comp.soldDate,
                "condition": comp.condition,
                "url": getattr(comp, "url", None),
            }
            for comp in comps
        ],
    }


def _source_name(pricing: Any) -> str | None:
    source = getattr(pricing, "pricingSource", None)
    if isinstance(source, dict):
        name = source.get("name")
        return name if isinstance(name, str) and name.strip() else None
    return None


def _display_currency_from_row(row: dict[str, Any]) -> str:
    raw = row.get("raw_json") or {}
    return (
        _clean(raw.get("currency"))
        or _clean((raw.get("pricing") or {}).get("currency"))
        or "AUD"
    )


def _year_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _clean(value: Any) -> str | None:
    parsed = str(value).strip() if value not in (None, "") else ""
    return parsed or None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
