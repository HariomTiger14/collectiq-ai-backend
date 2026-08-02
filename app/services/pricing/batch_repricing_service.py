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

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.pricing import RepriceIdentityRequest, RepriceRequest
from app.services.pricing.reprice_service import (
    RepriceService,
    RepriceValidationError,
)


@dataclass
class RepricingSummary:
    scanned: int = 0
    repriced: int = 0
    unavailable: int = 0
    skipped: int = 0
    dry_run: bool = False
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "repriced": self.repriced,
            "unavailable": self.unavailable,
            "skipped": self.skipped,
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
        request = _request_from_row(row)
        if request is None:
            summary.skipped += 1
            return
        try:
            response = self._reprice.reprice(request)
        except RepriceValidationError:
            summary.skipped += 1
            return
        except Exception as error:  # noqa: BLE001 - one bad row must not stop the sweep
            summary.errors.append({"id": row.get("id"), "error": str(error)})
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
        market = dict(raw.get("marketSummary") or {})
        market["lastUpdated"] = iso
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

    # --- Supabase access -----------------------------------------------------

    def _fetch_items_page(
        self, *, offset: int, page_size: int
    ) -> list[dict[str, Any]]:
        response = self._supabase_request(
            "GET",
            "/rest/v1/portfolio_items",
            params={
                "select": "id,user_id,category,title,manufacturer,series,year,"
                "raw_json,estimated_value_high,estimated_value_low",
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
    currency = (
        _clean(raw.get("currency"))
        or _clean((raw.get("pricing") or {}).get("currency"))
        or "AUD"
    )
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


def _source_name(pricing: Any) -> str | None:
    source = getattr(pricing, "pricingSource", None)
    if isinstance(source, dict):
        name = source.get("name")
        return name if isinstance(name, str) and name.strip() else None
    return None


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
