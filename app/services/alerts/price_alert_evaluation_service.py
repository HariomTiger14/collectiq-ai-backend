"""Server-side price-alert evaluation.

Reads a user's saved ``price_alerts`` (status=active) plus the current
``portfolio_items`` values and flips any alert whose condition is met to
``status='triggered'`` — the missing half of the notification pipeline (the
existing push service only *sends* already-triggered rows). Rule logic mirrors
the on-device evaluator (``price_alert_evaluator.dart``) so client and server
agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings


@dataclass
class EvaluationSummary:
    evaluated: int = 0
    triggered: int = 0
    rearmed: int = 0
    triggered_alerts: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "triggered": self.triggered,
            "rearmed": self.rearmed,
            "triggeredAlerts": self.triggered_alerts,
            "dryRun": self.dry_run,
        }


class PriceAlertEvaluationService:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        client: httpx.Client | None = None,
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

    def evaluate_and_flag(
        self,
        *,
        limit: int = 1000,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> EvaluationSummary:
        current_time = now or datetime.now(timezone.utc)
        alerts = self._fetch_active_alerts(limit=limit)
        if not alerts:
            return EvaluationSummary(dry_run=dry_run)

        user_ids = sorted({a["user_id"] for a in alerts if a.get("user_id")})
        items = self._fetch_items(user_ids)
        items_by_key = {
            (item.get("user_id"), item.get("id")): item for item in items
        }

        summary = EvaluationSummary(evaluated=len(alerts), dry_run=dry_run)
        for alert in alerts:
            item = items_by_key.get(
                (alert.get("user_id"), alert.get("portfolio_item_id"))
            )
            if item is None:
                continue
            message = self._evaluate(alert, item, current_time)
            status = alert.get("status")
            if status == "notified":
                # Already pushed. Re-arm to `active` once the condition clears so
                # it can trigger (and push) again later; otherwise leave it.
                if not message:
                    summary.rearmed += 1
                    if not dry_run:
                        self._rearm(alert, current_time)
                continue
            # status == "active"
            if not message:
                continue
            summary.triggered += 1
            summary.triggered_alerts.append(
                {
                    "id": alert.get("id"),
                    "userId": alert.get("user_id"),
                    "message": message,
                }
            )
            if not dry_run:
                self._flag_triggered(alert, message, current_time)
        return summary

    # --- Supabase access -----------------------------------------------------

    def _fetch_active_alerts(self, *, limit: int) -> list[dict[str, Any]]:
        response = self._supabase_request(
            "GET",
            "/rest/v1/price_alerts",
            params={
                "select": "*",
                "enabled": "eq.true",
                # `active` alerts may promote to triggered; `notified` alerts may
                # re-arm to active once their condition clears.
                "status": "in.(active,notified)",
                "limit": str(limit),
            },
        )
        data = response.json()
        return data if isinstance(data, list) else []

    def _fetch_items(self, user_ids: list[str]) -> list[dict[str, Any]]:
        if not user_ids:
            return []
        joined = ",".join(user_ids)
        response = self._supabase_request(
            "GET",
            "/rest/v1/portfolio_items",
            params={
                "select": "id,user_id,raw_json,estimated_value_high,"
                "estimated_value_low",
                "user_id": f"in.({joined})",
                # Soft-deleted items must not trigger price alerts.
                "sync_status": "neq.deleted",
            },
        )
        data = response.json()
        return data if isinstance(data, list) else []

    def _flag_triggered(
        self,
        alert: dict[str, Any],
        message: str,
        now: datetime,
    ) -> None:
        iso = now.isoformat()
        self._supabase_request(
            "PATCH",
            "/rest/v1/price_alerts",
            params={
                "id": f"eq.{alert['id']}",
                "user_id": f"eq.{alert['user_id']}",
            },
            headers={"Prefer": "return=minimal"},
            json={
                "status": "triggered",
                "triggered_at": alert.get("triggered_at") or iso,
                "message": message,
                "updated_at": iso,
            },
        )

    def _rearm(self, alert: dict[str, Any], now: datetime) -> None:
        """Reset a notified alert back to active once its condition clears, so a
        future crossing can trigger (and push) again."""
        iso = now.isoformat()
        self._supabase_request(
            "PATCH",
            "/rest/v1/price_alerts",
            params={
                "id": f"eq.{alert['id']}",
                "user_id": f"eq.{alert['user_id']}",
            },
            headers={"Prefer": "return=minimal"},
            json={
                "status": "active",
                "triggered_at": None,
                "notified_at": None,
                "message": None,
                "updated_at": iso,
            },
        )

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

    # --- Rule evaluation (mirrors price_alert_evaluator.dart) ----------------

    def _evaluate(
        self,
        alert: dict[str, Any],
        item: dict[str, Any],
        now: datetime,
    ) -> str | None:
        raw = item.get("raw_json") or {}
        current = _to_float(raw.get("estimatedValue"))
        if current is None:
            current = _to_float(item.get("estimated_value_high"))
        title = alert.get("item_title") or raw.get("title") or "Item"
        rule_type = alert.get("rule_type")

        if rule_type == "priceRisesAboveAmount":
            amount = _to_float(alert.get("target_amount"))
            if current is not None and amount is not None and current >= amount:
                return f"{title} rose above {_aud(amount)}."
        elif rule_type == "priceDropsBelowAmount":
            amount = _to_float(alert.get("target_amount"))
            if current is not None and amount is not None and current <= amount:
                return f"{title} dropped below {_aud(amount)}."
        elif rule_type == "percentageIncrease":
            baseline = _to_float(alert.get("baseline_value")) or current
            pct = _to_float(alert.get("percentage")) or 0.0
            if current is not None and baseline and baseline > 0:
                change = (current - baseline) / baseline
                if change >= pct:
                    return (
                        f"{title} gained {_percent(change)} since tracking "
                        "started."
                    )
        elif rule_type == "percentageDecrease":
            baseline = _to_float(alert.get("baseline_value")) or current
            pct = _to_float(alert.get("percentage")) or 0.0
            if current is not None and baseline and baseline > 0:
                change = (baseline - current) / baseline
                if change >= pct:
                    return (
                        f"{title} lost {_percent(change)} since tracking started."
                    )
        elif rule_type == "stalePricingReminder":
            last_updated = _last_updated(raw)
            if last_updated is None:
                return f"{title} needs fresh pricing data."
            stale_days = alert.get("stale_after_days") or 30
            if (now - last_updated).days >= stale_days:
                return f"{title} pricing is stale. Refresh market data."
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _last_updated(raw: dict[str, Any]) -> datetime | None:
    market = raw.get("marketSummary") or {}
    pricing = raw.get("pricing") or {}
    candidate = market.get("lastUpdated") or pricing.get("lastUpdated")
    return _parse_iso(candidate)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _aud(value: float) -> str:
    return f"AUD {int(round(value)):,}"


def _percent(value: float) -> str:
    return f"{round(value * 100)}%"
