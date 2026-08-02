"""Tests for server-side price-alert evaluation (flip active -> triggered)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.services.alerts.price_alert_evaluation_service import (
    PriceAlertEvaluationService,
)


def _service(
    *, alerts: list[dict[str, Any]], items: list[dict[str, Any]]
) -> tuple[PriceAlertEvaluationService, list[dict[str, Any]]]:
    patched: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/rest/v1/price_alerts":
            return httpx.Response(200, json=alerts)
        if request.method == "GET" and path == "/rest/v1/portfolio_items":
            return httpx.Response(200, json=items)
        if request.method == "PATCH" and path == "/rest/v1/price_alerts":
            patched.append(
                {
                    "params": dict(request.url.params),
                    "body": json.loads(request.content.decode()),
                }
            )
            return httpx.Response(204)
        return httpx.Response(404, json={})

    service = PriceAlertEvaluationService(
        supabase_url="https://example.supabase.co",
        service_role_key="service-role-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return service, patched


def _alert(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "alert-1",
        "user_id": "user-1",
        "portfolio_item_id": "item-1",
        "item_title": "Charizard",
        "rule_type": "priceRisesAboveAmount",
        "target_amount": None,
        "percentage": None,
        "baseline_value": None,
        "stale_after_days": None,
        "status": "active",
        "enabled": True,
        "triggered_at": None,
    }
    base.update(overrides)
    return base


def _item(value: float) -> dict[str, Any]:
    return {
        "id": "item-1",
        "user_id": "user-1",
        "raw_json": {"estimatedValue": value, "title": "Charizard"},
        "estimated_value_high": value,
    }


def test_rise_above_amount_triggers_and_patches():
    service, patched = _service(
        alerts=[_alert(rule_type="priceRisesAboveAmount", target_amount=100)],
        items=[_item(150)],
    )
    summary = service.evaluate_and_flag()

    assert summary.evaluated == 1
    assert summary.triggered == 1
    assert len(patched) == 1
    assert patched[0]["body"]["status"] == "triggered"
    assert "rose above" in patched[0]["body"]["message"]
    assert patched[0]["params"]["id"] == "eq.alert-1"


def test_rise_above_amount_not_met_does_not_patch():
    service, patched = _service(
        alerts=[_alert(rule_type="priceRisesAboveAmount", target_amount=100)],
        items=[_item(50)],
    )
    summary = service.evaluate_and_flag()

    assert summary.triggered == 0
    assert patched == []


def test_percentage_increase_triggers():
    service, patched = _service(
        alerts=[
            _alert(
                rule_type="percentageIncrease",
                percentage=0.10,
                baseline_value=100,
            )
        ],
        items=[_item(120)],  # +20% >= 10%
    )
    summary = service.evaluate_and_flag()

    assert summary.triggered == 1
    assert "gained" in patched[0]["body"]["message"]


def test_dry_run_reports_but_does_not_patch():
    service, patched = _service(
        alerts=[_alert(rule_type="priceRisesAboveAmount", target_amount=100)],
        items=[_item(150)],
    )
    summary = service.evaluate_and_flag(dry_run=True)

    assert summary.triggered == 1
    assert summary.dry_run is True
    assert patched == []


def test_notified_alert_rearms_when_condition_clears():
    service, patched = _service(
        alerts=[
            _alert(
                status="notified",
                rule_type="priceRisesAboveAmount",
                target_amount=100,
            )
        ],
        items=[_item(50)],  # no longer above 100 -> re-arm
    )
    summary = service.evaluate_and_flag()

    assert summary.triggered == 0
    assert summary.rearmed == 1
    assert len(patched) == 1
    assert patched[0]["body"]["status"] == "active"
    assert patched[0]["body"]["triggered_at"] is None
    assert patched[0]["body"]["notified_at"] is None


def test_notified_alert_stays_when_still_met():
    service, patched = _service(
        alerts=[
            _alert(
                status="notified",
                rule_type="priceRisesAboveAmount",
                target_amount=100,
            )
        ],
        items=[_item(150)],  # still above 100 -> stay notified, no re-push
    )
    summary = service.evaluate_and_flag()

    assert summary.triggered == 0
    assert summary.rearmed == 0
    assert patched == []
