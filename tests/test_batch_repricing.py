"""Tests for scheduled batch re-pricing (re-value + persist portfolio items)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.schemas.pricing import (
    RepriceIdentityRequest,
    RepricePricingResponse,
    RepriceRequest,
    RepriceResponse,
)
from app.services.pricing.base_pricing_provider import (
    PricingProviderRateLimitError,
)
from app.services.pricing.batch_repricing_service import BatchRepricingService


class _RateLimitedReprice:
    """Raises a throttle rate-limit for the first `fails` calls, then delegates
    to a wrapped fake (simulating PriceCharting's reject-if-too-fast throttle)."""

    def __init__(self, *, fails: int, inner: _FakeReprice):
        self._remaining_fails = fails
        self._inner = inner
        self.calls = 0

    def reprice(self, request: RepriceRequest) -> RepriceResponse:
        self.calls += 1
        if self._remaining_fails > 0:
            self._remaining_fails -= 1
            raise PricingProviderRateLimitError("throttled locally; retry")
        return self._inner.reprice(request)


class _FakeReprice:
    """Stand-in for RepriceService that returns a scripted pricing outcome."""

    def __init__(self, *, status: str, value: float | None, low=None, high=None):
        self._status = status
        self._value = value
        self._low = low
        self._high = high
        self.requests: list[RepriceRequest] = []

    def reprice(self, request: RepriceRequest) -> RepriceResponse:
        self.requests.append(request)
        pricing = RepricePricingResponse(
            status=self._status,
            estimatedMarketValue=self._value,
            lowEstimate=self._low,
            highEstimate=self._high,
            currency="AUD",
            confidenceScore=0.9,
            pricingConfidence=90,
            valuationStrategy="market_estimated"
            if self._status == "available"
            else "unavailable",
            pricingSource={"name": "MarketEngine", "attributionText": "", "lastChecked": ""},
        )
        return RepriceResponse(
            itemId=request.itemId,
            correctionSource=request.correctionSource,
            identity=request.identity,
            pricing=pricing,
        )


def _service(
    *, items_pages: list[list[dict[str, Any]]], reprice: _FakeReprice
) -> tuple[BatchRepricingService, list[dict[str, Any]], list[dict[str, Any]]]:
    patched: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    calls = {"page": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/rest/v1/portfolio_items":
            idx = calls["page"]
            calls["page"] += 1
            page = items_pages[idx] if idx < len(items_pages) else []
            return httpx.Response(200, json=page)
        if request.method == "PATCH" and path == "/rest/v1/portfolio_items":
            patched.append(
                {
                    "params": dict(request.url.params),
                    "body": json.loads(request.content.decode()),
                }
            )
            return httpx.Response(204)
        if request.method == "POST" and path == "/rest/v1/portfolio_valuation_snapshots":
            snapshots.append(json.loads(request.content.decode()))
            return httpx.Response(201)
        return httpx.Response(404, json={})

    service = BatchRepricingService(
        supabase_url="https://example.supabase.co",
        service_role_key="service-role-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        reprice_service=reprice,
        sleep=lambda _seconds: None,  # don't actually wait in tests
    )
    return service, patched, snapshots


def _item(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "item-1",
        "user_id": "user-1",
        "title": "Charizard",
        "category": "Pokemon",
        "manufacturer": "WOTC",
        "raw_json": {"title": "Charizard", "category": "Pokemon", "estimatedValue": 100},
        "estimated_value_high": 100,
        "estimated_value_low": 90,
    }
    base.update(overrides)
    return base


def test_available_reprice_persists_columns_and_raw_json():
    reprice = _FakeReprice(status="available", value=250.0, low=200.0, high=300.0)
    service, patched, _snap = _service(items_pages=[[_item()]], reprice=reprice)

    summary = service.reprice_all(page_size=200)

    assert summary.scanned == 1
    assert summary.repriced == 1
    assert summary.unavailable == 0
    assert len(patched) == 1
    body = patched[0]["body"]
    assert body["estimated_value_low"] == 200.0
    assert body["estimated_value_high"] == 300.0
    assert body["raw_json"]["estimatedValue"] == 250.0
    assert body["raw_json"]["valuationStatus"] == "market_estimated"
    assert body["raw_json"]["pricing"]["estimatedMarketValue"] == 250.0
    assert patched[0]["params"]["id"] == "eq.item-1"
    assert patched[0]["params"]["user_id"] == "eq.user-1"


def test_available_reprice_writes_a_valuation_snapshot():
    reprice = _FakeReprice(status="available", value=250.0, low=200.0, high=300.0)
    service, _patched, snapshots = _service(
        items_pages=[[_item()]], reprice=reprice
    )

    service.reprice_all()

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot["user_id"] == "user-1"
    assert snapshot["portfolio_item_id"] == "item-1"
    assert snapshot["value_aud"] == 250.0
    assert snapshot["low_estimate_aud"] == 200.0
    assert snapshot["high_estimate_aud"] == 300.0
    assert snapshot["valuation_status"] == "market_estimated"
    assert snapshot["pricing_provider"] == "MarketEngine"
    assert snapshot["confidence_score"] == 0.9
    assert snapshot["priced_at"]


def test_unavailable_reprice_writes_no_snapshot():
    reprice = _FakeReprice(status="unavailable", value=None)
    service, _patched, snapshots = _service(
        items_pages=[[_item()]], reprice=reprice
    )

    service.reprice_all()

    assert snapshots == []


def test_dry_run_writes_no_snapshot():
    reprice = _FakeReprice(status="available", value=250.0, low=200.0, high=300.0)
    service, _patched, snapshots = _service(
        items_pages=[[_item()]], reprice=reprice
    )

    service.reprice_all(dry_run=True)

    assert snapshots == []


def test_snapshot_write_failure_does_not_block_reprice_summary():
    """The snapshot table might reject a row (e.g. RLS/network hiccup) — that
    must never cause the item's own repriced value to be dropped."""

    reprice = _FakeReprice(status="available", value=250.0, low=200.0, high=300.0)
    patched: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/rest/v1/portfolio_items":
            return httpx.Response(200, json=[_item()])
        if request.method == "PATCH" and path == "/rest/v1/portfolio_items":
            patched.append(json.loads(request.content.decode()))
            return httpx.Response(204)
        if request.method == "POST" and path == "/rest/v1/portfolio_valuation_snapshots":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(404, json={})

    service = BatchRepricingService(
        supabase_url="https://example.supabase.co",
        service_role_key="service-role-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        reprice_service=reprice,
        sleep=lambda _seconds: None,
    )

    summary = service.reprice_all()

    assert summary.repriced == 1
    assert len(patched) == 1


def test_unavailable_reprice_is_a_noop():
    reprice = _FakeReprice(status="unavailable", value=None)
    service, patched, _snap = _service(items_pages=[[_item()]], reprice=reprice)

    summary = service.reprice_all()

    assert summary.scanned == 1
    assert summary.repriced == 0
    assert summary.unavailable == 1
    assert patched == []


def test_zero_value_available_does_not_persist():
    reprice = _FakeReprice(status="available", value=0.0)
    service, patched, _snap = _service(items_pages=[[_item()]], reprice=reprice)

    summary = service.reprice_all()

    assert summary.repriced == 0
    assert summary.unavailable == 1
    assert patched == []


def test_missing_identity_is_skipped():
    reprice = _FakeReprice(status="available", value=250.0)
    bad = _item(title="", category="", raw_json={"estimatedValue": 100})
    service, patched, _snap = _service(items_pages=[[bad]], reprice=reprice)

    summary = service.reprice_all()

    assert summary.scanned == 1
    assert summary.skipped == 1
    assert summary.repriced == 0
    assert patched == []
    assert reprice.requests == []


def test_dry_run_reports_but_does_not_patch():
    reprice = _FakeReprice(status="available", value=250.0, low=200.0, high=300.0)
    service, patched, _snap = _service(items_pages=[[_item()]], reprice=reprice)

    summary = service.reprice_all(dry_run=True)

    assert summary.repriced == 1
    assert summary.dry_run is True
    assert patched == []


def test_pagination_stops_on_short_page():
    reprice = _FakeReprice(status="available", value=250.0, low=200.0, high=300.0)
    page1 = [_item(id=f"item-{i}") for i in range(2)]
    service, patched, _snap = _service(items_pages=[page1, []], reprice=reprice)

    summary = service.reprice_all(page_size=2, limit=10)

    # Second page is empty -> loop stops; both rows on page 1 repriced.
    assert summary.scanned == 2
    assert summary.repriced == 2
    assert len(patched) == 2


def test_identity_built_from_raw_json_fields():
    reprice = _FakeReprice(status="available", value=250.0)
    item = _item(
        raw_json={
            "title": "Blastoise",
            "category": "Pokemon",
            "brand": "WOTC",
            "setName": "Base Set",
            "year": "1999",
            "condition": "Near Mint",
            "estimatedValue": 100,
        }
    )
    service, _, _snap = _service(items_pages=[[item]], reprice=reprice)

    service.reprice_all()

    assert len(reprice.requests) == 1
    identity: RepriceIdentityRequest = reprice.requests[0].identity
    assert identity.title == "Blastoise"
    assert identity.setName == "Base Set"
    assert identity.year == "1999"
    assert reprice.requests[0].correctionSource == "scheduled_reprice"


def test_rate_limited_then_succeeds_is_repriced():
    inner = _FakeReprice(status="available", value=250.0, low=200.0, high=300.0)
    reprice = _RateLimitedReprice(fails=2, inner=inner)  # throttled twice, then ok
    service, patched, _snap = _service(items_pages=[[_item()]], reprice=reprice)

    summary = service.reprice_all()

    assert summary.repriced == 1
    assert summary.rate_limited == 2
    assert reprice.calls == 3  # 2 rejections + 1 success
    assert len(patched) == 1


def test_rate_limited_beyond_retries_records_error_not_repriced():
    inner = _FakeReprice(status="available", value=250.0)
    reprice = _RateLimitedReprice(fails=99, inner=inner)  # always throttled
    service, patched, _snap = _service(items_pages=[[_item()]], reprice=reprice)

    summary = service.reprice_all()

    assert summary.repriced == 0
    assert summary.unavailable == 0
    assert summary.rate_limited == 3  # default max_rate_limit_retries
    assert len(summary.errors) == 1
    assert patched == []
