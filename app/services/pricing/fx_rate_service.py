from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings

# Currencies actually offered in the app's display-currency picker, minus
# USD itself (USD is always 1.0 by definition -- never stored as a row).
# Matches SUPPORTED_DISPLAY_CURRENCIES in currency_conversion.py.
TRACKED_CURRENCIES = ("AUD", "CAD", "GBP")

# Frankfurter is a free, no-API-key exchange rate API backed by the
# European Central Bank's own daily reference rates -- no vendor
# relationship or paid tier needed at PackLox's scale (one fetch/day).
_FRANKFURTER_BASE = "https://api.frankfurter.dev/v1"


class FxRateServiceError(Exception):
    """Raised when a Frankfurter fetch or a Supabase read/write fails.
    Callers (the admin refresh endpoint, the public rates endpoint) decide
    whether that's fatal -- a missed daily refresh isn't: currency_
    conversion.py's static env-var rates remain the fallback for any date
    this table has no row for yet."""


class FxRateService:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).strip().rstrip("/")
        self._service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        ).strip()
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else 30
        )
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def refresh_latest(self) -> int:
        """Fetches today's USD-base rates from Frankfurter and upserts them.
        Meant to be called once a day by a Render cron. Frankfurter (like
        the ECB rates it mirrors) only publishes on days European markets
        are open -- get_rate()'s "most recent earlier date" fallback is
        what covers weekends/holidays, not this call needing to run daily
        without fail."""
        payload = self._frankfurter_get(
            f"{_FRANKFURTER_BASE}/latest",
            params={"base": "USD", "symbols": ",".join(TRACKED_CURRENCIES)},
        )
        rate_date = payload.get("date")
        rates = payload.get("rates") or {}
        if not rate_date or not isinstance(rates, dict):
            raise FxRateServiceError("Frankfurter response missing date/rates.")
        rows = [
            {"rate_date": rate_date, "currency": currency, "usd_rate": rate}
            for currency, rate in rates.items()
            if currency in TRACKED_CURRENCIES
        ]
        return self._upsert(rows)

    def backfill_historical(self, *, start_date: str, end_date: str) -> int:
        """One-time (or periodic, re-runnable) backfill of a date range from
        Frankfurter's time-series endpoint, so historical chart points can
        be converted using the rate that was actually in effect on that
        date instead of falling back to the static env-var rate. Safe to
        re-run -- upsert on (rate_date, currency)."""
        payload = self._frankfurter_get(
            f"{_FRANKFURTER_BASE}/{start_date}..{end_date}",
            params={"base": "USD", "symbols": ",".join(TRACKED_CURRENCIES)},
        )
        rates_by_date = payload.get("rates") or {}
        if not isinstance(rates_by_date, dict):
            raise FxRateServiceError("Frankfurter time-series response missing rates.")
        rows: list[dict[str, Any]] = []
        for day, day_rates in rates_by_date.items():
            if not isinstance(day_rates, dict):
                continue
            for currency, rate in day_rates.items():
                if currency in TRACKED_CURRENCIES:
                    rows.append(
                        {"rate_date": day, "currency": currency, "usd_rate": rate}
                    )
        return self._upsert(rows)

    def rates_for_range(
        self, *, currencies: list[str], from_date: str, to_date: str
    ) -> list[dict[str, Any]]:
        """Every stored (date, currency, usd_rate) row in the range, for the
        mobile app's own historical-chart conversion. Deliberately returns
        raw rows rather than a per-day-per-currency-guaranteed grid --
        Frankfurter has no row for non-trading days, and the client applies
        the same "most recent earlier date" fallback get_rate() uses below,
        so a sparse list is the correct shape, not a bug to work around
        here."""
        normalized = [c.strip().upper() for c in currencies if c.strip()]
        normalized = [c for c in normalized if c in TRACKED_CURRENCIES]
        if not normalized:
            return []
        payload = self._supabase_request(
            "GET",
            "/rest/v1/fx_rates_daily",
            params=[
                ("select", "rate_date,currency,usd_rate"),
                ("currency", f"in.({','.join(normalized)})"),
                ("rate_date", f"gte.{from_date}"),
                ("rate_date", f"lte.{to_date}"),
                ("order", "rate_date.asc"),
                ("limit", "10000"),
            ],
        )
        return payload if isinstance(payload, list) else []

    def get_rate(self, currency: str, rate_date: str) -> float | None:
        """The rate for `currency` on `rate_date`, falling back to the most
        recent earlier stored date (the standard convention for
        weekends/holidays a market didn't publish a rate for). Returns None
        if nothing is stored on or before that date at all -- the caller
        (currency_conversion.py) falls back to the static env-var rate in
        that case, same as before this table existed."""
        normalized = currency.strip().upper()
        if normalized == "USD":
            return 1.0
        if normalized not in TRACKED_CURRENCIES:
            return None
        payload = self._supabase_request(
            "GET",
            "/rest/v1/fx_rates_daily",
            params={
                "select": "usd_rate",
                "currency": f"eq.{normalized}",
                "rate_date": f"lte.{rate_date}",
                "order": "rate_date.desc",
                "limit": "1",
            },
        )
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        rate = row.get("usd_rate") if isinstance(row, dict) else None
        return float(rate) if rate is not None else None

    def current_rates(self) -> dict[str, float]:
        """USD is always 1.0. AUD/CAD/GBP come from the most recent stored
        row per currency; a currency with no row yet is simply omitted --
        the caller (the /api/pricing/fx-rates endpoint) fills gaps from the
        static env-var rates, same fallback posture as get_rate()."""
        rates: dict[str, float] = {"USD": 1.0}
        for currency in TRACKED_CURRENCIES:
            payload = self._supabase_request(
                "GET",
                "/rest/v1/fx_rates_daily",
                params={
                    "select": "usd_rate",
                    "currency": f"eq.{currency}",
                    "order": "rate_date.desc",
                    "limit": "1",
                },
            )
            if isinstance(payload, list) and payload:
                rate = payload[0].get("usd_rate") if isinstance(payload[0], dict) else None
                if rate is not None:
                    rates[currency] = float(rate)
        return rates

    def _upsert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        self._supabase_request(
            "POST",
            "/rest/v1/fx_rates_daily",
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=rows,
        )
        return len(rows)

    def _frankfurter_get(self, url: str, *, params: dict[str, str]) -> dict[str, Any]:
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.get(url, params=params, timeout=self._timeout_seconds)
        except httpx.HTTPError as error:
            raise FxRateServiceError(f"Frankfurter request failed: {error}") from error
        finally:
            if should_close:
                client.close()
        if response.status_code >= 400:
            raise FxRateServiceError(
                f"Frankfurter returned HTTP {response.status_code}: {response.text}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise FxRateServiceError("Frankfurter response was not valid JSON.") from error
        if not isinstance(payload, dict):
            raise FxRateServiceError("Frankfurter response shape was invalid.")
        return payload

    def _supabase_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
    ) -> Any:
        if not self.is_configured:
            raise FxRateServiceError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required."
            )
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.request(
                method,
                f"{self._supabase_url}{path}",
                params=params,
                headers={
                    "apikey": self._service_role_key,
                    "Authorization": f"Bearer {self._service_role_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    **(headers or {}),
                },
                json=json,
            )
        except httpx.HTTPError as error:
            raise FxRateServiceError(f"Supabase request failed: {error}") from error
        finally:
            if should_close:
                client.close()
        if response.status_code >= 400:
            raise FxRateServiceError(
                f"Supabase returned HTTP {response.status_code}: {response.text}"
            )
        if not response.content:
            return None
        return response.json()
