from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import settings
from scripts.import_pricecharting_catalog import PRICECHARTING_CSV_ENV_VARS


class PricingHealthError(Exception):
    """Raised when pricing health cannot be checked."""


@dataclass(frozen=True)
class PricingHealthService:
    supabase_url: str | None = None
    service_role_key: str | None = None
    timeout_seconds: float = 5
    stale_after_hours: int = 36
    client: httpx.Client | None = None

    def health(self) -> dict[str, Any]:
        generated_at = datetime.now(timezone.utc)
        catalog_configured = bool(self._supabase_url and self._service_role_key)
        sources: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        if catalog_configured:
            for source_key in sorted(PRICECHARTING_CSV_ENV_VARS):
                source_file = f"{source_key}.csv"
                try:
                    sources.append(self._source_health(source_file, generated_at))
                except PricingHealthError as error:
                    sources.append(
                        {
                            "source": source_file,
                            "status": "unhealthy",
                            "currentRows": 0,
                            "historyRows": 0,
                            "closedHistoryRows": 0,
                            "lastLoadedAt": None,
                            "ageHours": None,
                            "stale": True,
                            "error": str(error),
                        }
                    )
                    errors.append({"source": source_file, "message": str(error)})

        provider_statuses = _provider_statuses(catalog_configured)
        currency_status = _currency_status()
        stale_sources = [
            source for source in sources if bool(source.get("stale")) is True
        ]
        missing_sources = [
            source for source in sources if int(source.get("currentRows") or 0) <= 0
        ]

        status = "healthy"
        if not catalog_configured:
            status = "unhealthy"
        elif errors or missing_sources:
            status = "unhealthy"
        elif stale_sources:
            status = "degraded"

        return {
            "success": True,
            "status": status,
            "generatedAt": generated_at.isoformat(),
            "summary": {
                "status": status,
                "catalogConfigured": catalog_configured,
                "sourceCount": len(sources),
                "staleSourceCount": len(stale_sources),
                "missingSourceCount": len(missing_sources),
                "configuredProviderCount": sum(
                    1 for provider in provider_statuses if provider["configured"]
                ),
                "errors": errors,
            },
            "pricecharting": {
                "status": "configured" if catalog_configured else "not_configured",
                "staleAfterHours": self.stale_after_hours,
                "sources": sources,
                "totalCurrentRows": sum(
                    int(source.get("currentRows") or 0) for source in sources
                ),
                "totalHistoryRows": sum(
                    int(source.get("historyRows") or 0) for source in sources
                ),
                "totalClosedHistoryRows": sum(
                    int(source.get("closedHistoryRows") or 0) for source in sources
                ),
            },
            "providers": provider_statuses,
            "currency": currency_status,
        }

    @property
    def _supabase_url(self) -> str:
        value = self.supabase_url if self.supabase_url is not None else settings.supabase_url
        return value.strip().rstrip("/")

    @property
    def _service_role_key(self) -> str:
        value = (
            self.service_role_key
            if self.service_role_key is not None
            else settings.supabase_service_role_key
        )
        return value.strip()

    def _source_health(
        self,
        source_file: str,
        generated_at: datetime,
    ) -> dict[str, Any]:
        current_rows = self._count(
            table="pricecharting_catalog",
            filters={"source_file": f"eq.{source_file}"},
        )
        history_rows = self._count(
            table="pricecharting_catalog_history",
            filters={"source_file": f"eq.{source_file}"},
        )
        closed_history_rows = self._count(
            table="pricecharting_catalog_history",
            filters={
                "source_file": f"eq.{source_file}",
                "is_current": "eq.false",
            },
        )
        latest = self._latest_catalog_row(source_file)
        last_loaded_at = _latest_timestamp(latest)
        age_hours = _age_hours(last_loaded_at, generated_at)
        stale = age_hours is None or age_hours > self.stale_after_hours
        source_status = "healthy"
        if current_rows <= 0:
            source_status = "unhealthy"
        elif stale:
            source_status = "stale"

        return {
            "source": source_file,
            "status": source_status,
            "currentRows": current_rows,
            "historyRows": history_rows,
            "closedHistoryRows": closed_history_rows,
            "lastLoadedAt": last_loaded_at.isoformat() if last_loaded_at else None,
            "ageHours": age_hours,
            "stale": stale,
        }

    def _latest_catalog_row(self, source_file: str) -> dict[str, Any] | None:
        payload = self._request(
            "GET",
            "/rest/v1/pricecharting_catalog",
            params={
                "select": "source_downloaded_at,imported_at,updated_at",
                "source_file": f"eq.{source_file}",
                "order": "imported_at.desc",
                "limit": "1",
            },
        )
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        return None

    def _count(self, *, table: str, filters: dict[str, str]) -> int:
        response = self._raw_request(
            "GET",
            f"/rest/v1/{table}",
            params={"select": "pricecharting_id", **filters},
            extra_headers={"Prefer": "count=exact", "Range": "0-0"},
        )
        return _count_from_content_range(response.headers.get("content-range", ""))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        response = self._raw_request(method, path, params=params)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as error:
            raise PricingHealthError("Supabase health response was not JSON.") from error

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        client = self.client or httpx.Client(timeout=self.timeout_seconds)
        should_close = self.client is None
        try:
            response = client.request(
                method,
                f"{self._supabase_url}{path}",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as error:
            raise PricingHealthError("Supabase pricing health request failed.") from error
        finally:
            if should_close:
                client.close()


def _provider_statuses(catalog_configured: bool) -> list[dict[str, Any]]:
    ebay_configured = bool(
        settings.ebay_access_token.strip()
        or (
            settings.ebay_client_id.strip()
            and settings.ebay_client_secret.strip()
        )
    )
    return [
        {
            "name": "PriceCharting Catalog",
            "key": "pricecharting_catalog",
            "configured": catalog_configured,
            "status": "configured" if catalog_configured else "not_configured",
            "role": "primary_catalog",
        },
        {
            "name": "PriceCharting API",
            "key": "pricecharting_api",
            "configured": bool(settings.pricecharting_api_key.strip()),
            "status": "configured"
            if settings.pricecharting_api_key.strip()
            else "not_configured",
            "role": "direct_api_optional",
        },
        {
            "name": "eBay",
            "key": "ebay",
            "configured": ebay_configured,
            "status": "configured" if ebay_configured else "pending",
            "role": "sold_comps_future",
            "marketplace": settings.ebay_marketplace_id,
        },
        {
            "name": "TCGplayer",
            "key": "tcgplayer",
            "configured": bool(
                settings.tcgplayer_client_id.strip()
                and settings.tcgplayer_client_secret.strip()
            ),
            "status": "configured"
            if settings.tcgplayer_client_id.strip()
            and settings.tcgplayer_client_secret.strip()
            else "not_configured",
            "role": "trading_cards_future",
        },
    ]


def _currency_status() -> dict[str, Any]:
    rates = {
        "USD": 1,
        "AUD": settings.fx_usd_to_aud,
        "CAD": settings.fx_usd_to_cad,
        "GBP": settings.fx_usd_to_gbp,
    }
    valid = all(rate > 0 for rate in rates.values())
    return {
        "status": "configured" if valid else "invalid",
        "defaultDisplayCurrency": settings.default_display_currency.strip().upper()
        or "AUD",
        "sourceCurrency": "USD",
        "rates": rates,
        "cache": {
            "status": "static_env",
            "note": "Rates are read from backend environment variables.",
        },
    }


def _latest_timestamp(row: dict[str, Any] | None) -> datetime | None:
    if not row:
        return None
    timestamps = [
        _parse_datetime(row.get("source_downloaded_at")),
        _parse_datetime(row.get("imported_at")),
        _parse_datetime(row.get("updated_at")),
    ]
    valid = [timestamp for timestamp in timestamps if timestamp is not None]
    if not valid:
        return None
    return max(valid)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_hours(timestamp: datetime | None, generated_at: datetime) -> int | None:
    if timestamp is None:
        return None
    age = generated_at - timestamp
    if age < timedelta(0):
        return 0
    return round(age.total_seconds() / 3600)


def _count_from_content_range(value: str) -> int:
    if "/" not in value:
        return 0
    count = value.rsplit("/", 1)[-1].strip()
    if count == "*":
        return 0
    try:
        return int(count)
    except ValueError:
        return 0
