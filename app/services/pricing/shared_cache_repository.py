from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from app.core.config import settings
from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.base_pricing_provider import PricingResult, utc_timestamp
from app.services.pricing.cache_policy import PricingCachePolicy, pricing_cache_policy


class SharedPricingCacheError(Exception):
    """Raised when the shared pricing cache cannot be read or written."""


class SharedPricingCacheRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        timeout_seconds: float = 5,
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
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def cache_key(self, recognition: RecognitionResult) -> str:
        identity = _normalized_identity(recognition)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"pricing:{digest}"

    def get(self, recognition: RecognitionResult) -> PricingResult | None:
        if not self.is_configured:
            return None

        cache_key = self.cache_key(recognition)
        now = datetime.now(timezone.utc).isoformat()
        params = {
            "cache_key": f"eq.{cache_key}",
            "expires_at": f"gt.{now}",
            "select": "*",
            "limit": "1",
        }
        payload = self._request("GET", "/rest/v1/pricing_cache_entries", params=params)
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        if not isinstance(row, dict):
            return None
        self._increment_hit_count(cache_key)
        return _pricing_result_from_row(row)

    def set(self, recognition: RecognitionResult, pricing: PricingResult) -> None:
        if not self.is_configured:
            return

        policy = pricing_cache_policy(
            category=recognition.category,
            valuation_status=pricing.valuationStatus,
        )
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=policy.ttl_seconds)
        row = _row_from_pricing(
            cache_key=self.cache_key(recognition),
            recognition=recognition,
            pricing=pricing,
            policy=policy,
            checked_at=now,
            expires_at=expires_at,
        )
        self._request(
            "POST",
            "/rest/v1/pricing_cache_entries",
            params={"on_conflict": "cache_key"},
            json_payload=row,
            extra_headers={"Prefer": "resolution=merge-duplicates"},
        )

    def _increment_hit_count(self, cache_key: str) -> None:
        # Best-effort only. A failed counter update must not block valuation.
        try:
            self._request(
                "POST",
                "/rest/v1/rpc/increment_pricing_cache_hit",
                json_payload={"cache_key_arg": cache_key},
                extra_headers={"Prefer": "return=minimal"},
            )
        except SharedPricingCacheError:
            return

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_payload: dict | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.request(
                method,
                f"{self._supabase_url}{path}",
                headers=headers,
                params=params,
                json=json_payload,
            )
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise SharedPricingCacheError("Shared pricing cache request failed.") from error
        finally:
            if should_close:
                client.close()


def _normalized_identity(recognition: RecognitionResult) -> str:
    parts = [
        recognition.category,
        recognition.title,
        recognition.brand,
        recognition.setName,
        recognition.series,
        recognition.year,
        recognition.cardNumber,
        recognition.playerOrCharacter,
        recognition.edition,
        recognition.condition,
    ]
    return "|".join(
        " ".join(str(part).strip().lower().split())
        for part in parts
        if isinstance(part, str) and part.strip()
    )


def _row_from_pricing(
    *,
    cache_key: str,
    recognition: RecognitionResult,
    pricing: PricingResult,
    policy: PricingCachePolicy,
    checked_at: datetime,
    expires_at: datetime,
) -> dict:
    diagnostics = pricing.providerDiagnostics
    return {
        "cache_key": cache_key,
        "category": recognition.category or "Collectible",
        "normalized_identity": _normalized_identity(recognition),
        "condition_label": recognition.condition or None,
        "valuation_status": pricing.valuationStatus,
        "value_aud": _nullable_number(pricing.estimatedMarketValue),
        "low_estimate_aud": _nullable_number(pricing.lowEstimate),
        "high_estimate_aud": _nullable_number(pricing.highEstimate),
        "display_string": f"${pricing.estimatedMarketValue:,.2f} {pricing.currency}"
        if pricing.estimatedMarketValue
        else None,
        "valuation_strategy": "sold_completed"
        if pricing.valuationStatus == "market_estimated"
        else "unavailable",
        "pricing_provider": pricing.valuationSource,
        "attribution_text": f"Pricing data powered by {pricing.valuationSource}"
        if pricing.valuationStatus == "market_estimated"
        else None,
        "confidence_score": _confidence_score(pricing.pricingConfidence),
        "reason_code": None
        if pricing.valuationStatus == "market_estimated"
        else pricing.valuationStatus.upper(),
        "match_reason": diagnostics.get("priceExplanation")
        or diagnostics.get("confidenceCalculation"),
        "original_price": _nullable_number(pricing.estimatedMarketValue),
        "original_currency": pricing.currency,
        "exchange_rate_used": 1,
        "exchange_rate_date": pricing.lastUpdated,
        "checked_at": checked_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "evidence_json": {
            "cachePolicyReason": policy.reason,
            "sourceCount": pricing.sourceCount,
            "pricingAge": pricing.pricingAge,
            "cacheStatus": pricing.cacheStatus,
        },
    }


def _pricing_result_from_row(row: dict) -> PricingResult:
    evidence = row.get("evidence_json") if isinstance(row.get("evidence_json"), dict) else {}
    provider = str(row.get("pricing_provider") or "shared_cache")
    match_reason = str(row.get("match_reason") or "Served from PackLox shared pricing cache.")
    return PricingResult(
        estimatedMarketValue=_int_number(row.get("value_aud")),
        lowEstimate=_int_number(row.get("low_estimate_aud")),
        highEstimate=_int_number(row.get("high_estimate_aud")),
        currency="AUD",
        pricingSource=provider,
        pricingConfidence=_confidence_int(row.get("confidence_score")),
        lastUpdated=str(row.get("checked_at") or utc_timestamp()),
        valuationStatus=str(row.get("valuation_status") or "market_estimated"),
        valuationSource=provider,
        marketTrend="Stable",
        sourceCount=int(evidence.get("sourceCount") or 1),
        pricingAge=str(evidence.get("pricingAge") or "cached"),
        comparableSales=[],
        fallbackUsed=False,
        cacheStatus="shared_hit",
        providerDiagnostics={
            "providerCount": str(evidence.get("sourceCount") or 1),
            "providers": provider,
            "fallbackUsed": "false",
            "cacheStatus": "shared_hit",
            "responseTimeMs": "0",
            "comparableCount": "0",
            "confidenceCalculation": match_reason,
            "priceExplanation": match_reason,
        },
    )


def with_shared_cache_status(pricing: PricingResult, cache_status: str) -> PricingResult:
    return replace(
        pricing,
        cacheStatus=cache_status,
        providerDiagnostics={
            **pricing.providerDiagnostics,
            "cacheStatus": cache_status,
        },
    )


def _nullable_number(value: int | float | Decimal | None) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if parsed > 0 else None


def _int_number(value) -> int:
    if value is None:
        return 0
    return max(0, round(float(value)))


def _confidence_score(value) -> float:
    parsed = float(value or 0)
    return parsed / 100 if parsed > 1 else parsed


def _confidence_int(value) -> int:
    parsed = float(value or 0)
    return round(parsed * 100 if parsed <= 1 else parsed)
