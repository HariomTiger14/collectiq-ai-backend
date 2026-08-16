from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from app.core.config import settings


class PushNotificationError(RuntimeError):
    """Raised when the push notification job cannot run safely."""


# A device with no last_seen_at, or one that hasn't checked in for this many
# days, counts as part of the "inactive" broadcast audience segment.
INACTIVE_DEVICE_DAYS = 30


@dataclass(frozen=True)
class PushDeliverySummary:
    success: bool
    scanned_alerts: int
    attempted_deliveries: int
    sent_deliveries: int
    failed_deliveries: int
    skipped_deliveries: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "scannedAlerts": self.scanned_alerts,
            "attemptedDeliveries": self.attempted_deliveries,
            "sentDeliveries": self.sent_deliveries,
            "failedDeliveries": self.failed_deliveries,
            "skippedDeliveries": self.skipped_deliveries,
        }


@dataclass(frozen=True)
class TargetedPushSummary:
    success: bool
    segment: str
    audience_devices: int
    attempted_deliveries: int
    sent_deliveries: int
    failed_deliveries: int
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "segment": self.segment,
            "audienceDevices": self.audience_devices,
            "attemptedDeliveries": self.attempted_deliveries,
            "sentDeliveries": self.sent_deliveries,
            "failedDeliveries": self.failed_deliveries,
            "skippedDeliveries": self.attempted_deliveries if self.dry_run else 0,
            "dryRun": self.dry_run,
        }


class PriceAlertPushService:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        firebase_project_id: str | None = None,
        firebase_service_account_json: str | None = None,
        firebase_access_token: str | None = None,
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
        self._firebase_project_id = (
            firebase_project_id
            if firebase_project_id is not None
            else settings.firebase_project_id
        )
        self._firebase_service_account_json = (
            firebase_service_account_json
            if firebase_service_account_json is not None
            else settings.firebase_service_account_json
        )
        self._firebase_access_token = (
            firebase_access_token
            if firebase_access_token is not None
            else settings.firebase_access_token
        )
        self._client = client or httpx.Client(timeout=20)

    def dispatch_triggered_alerts(
        self,
        *,
        limit: int = 50,
        dry_run: bool = False,
    ) -> PushDeliverySummary:
        self._ensure_configured(require_firebase=not dry_run)
        alerts = self._fetch_triggered_alerts(limit=limit)

        attempted = sent = failed = skipped = 0
        for alert in alerts:
            devices = self._fetch_devices(str(alert.get("user_id") or ""))
            if not devices:
                skipped += 1
                if not dry_run:
                    self._log_delivery(
                        alert,
                        None,
                        "skipped",
                        error_message="No registered push device.",
                    )
                continue

            alert_sent = False
            for device in devices:
                attempted += 1
                if dry_run:
                    skipped += 1
                    continue
                status, provider_message_id, error_message = self._send_fcm(alert, device)
                if status == "sent":
                    sent += 1
                    alert_sent = True
                else:
                    failed += 1
                self._log_delivery(
                    alert,
                    device,
                    status,
                    provider_message_id=provider_message_id,
                    error_message=error_message,
                )

            # Notify-once: once a push has gone out for this trigger, move the
            # alert out of `triggered` so the next scheduled run does not re-push
            # it. The evaluator re-arms it to `active` when the condition clears.
            if alert_sent and not dry_run:
                self._mark_notified(alert)

        return PushDeliverySummary(
            success=failed == 0,
            scanned_alerts=len(alerts),
            attempted_deliveries=attempted,
            sent_deliveries=sent,
            failed_deliveries=failed,
            skipped_deliveries=skipped,
        )

    def dispatch_test_notification(
        self,
        *,
        user_id: str | None = None,
        limit: int = 10,
        dry_run: bool = False,
    ) -> PushDeliverySummary:
        self._ensure_configured(require_firebase=not dry_run)
        devices = self._fetch_enabled_devices(user_id=user_id, limit=limit)

        attempted = sent = failed = skipped = 0
        for device in devices:
            attempted += 1
            if dry_run:
                skipped += 1
                continue
            status, _provider_message_id, _error_message = self._send_test_fcm(device)
            if status == "sent":
                sent += 1
            else:
                failed += 1

        return PushDeliverySummary(
            success=failed == 0,
            scanned_alerts=0,
            attempted_deliveries=attempted,
            sent_deliveries=sent,
            failed_deliveries=failed,
            skipped_deliveries=skipped,
        )

    def dispatch_test_price_alert_notification(
        self,
        *,
        portfolio_item_id: str,
        user_id: str | None = None,
        limit: int = 10,
        dry_run: bool = False,
    ) -> PushDeliverySummary:
        self._ensure_configured(require_firebase=not dry_run)
        devices = self._fetch_enabled_devices(user_id=user_id, limit=limit)
        alert = {
            "id": "admin-test-price-alert",
            "user_id": user_id,
            "portfolio_item_id": portfolio_item_id,
            "item_title": "PackLox test item",
            "message": "Open this alert to review your collectible.",
        }

        attempted = sent = failed = skipped = 0
        for device in devices:
            attempted += 1
            if dry_run:
                skipped += 1
                continue
            status, _provider_message_id, _error_message = self._send_fcm(
                alert,
                device,
            )
            if status == "sent":
                sent += 1
            else:
                failed += 1

        return PushDeliverySummary(
            success=failed == 0,
            scanned_alerts=0,
            attempted_deliveries=attempted,
            sent_deliveries=sent,
            failed_deliveries=failed,
            skipped_deliveries=skipped,
        )

    def audience_counts(self) -> dict[str, Any]:
        self._ensure_configured(require_firebase=False)
        devices = self._fetch_all_enabled_devices()
        pro_user_ids = self._fetch_pro_user_ids()
        now = datetime.now(timezone.utc)
        return {
            "all": len(devices),
            "pro": sum(1 for d in devices if d.get("user_id") in pro_user_ids),
            "inactive": sum(1 for d in devices if self._is_inactive_device(d, now)),
            "inactiveDays": INACTIVE_DEVICE_DAYS,
        }

    def dispatch_broadcast(
        self,
        *,
        segment: str,
        title: str,
        body: str,
        dry_run: bool = True,
        limit: int = 2000,
    ) -> TargetedPushSummary:
        self._ensure_configured(require_firebase=not dry_run)
        title = title.strip()
        body = body.strip()
        if not title or not body:
            raise PushNotificationError("Broadcast title and message are required.")
        if not dry_run:
            last = self._last_broadcast_today()
            if last:
                raise PushNotificationError(
                    "Only one live broadcast is allowed per day — one was "
                    f"already sent at {last.get('created_at')}."
                )
        devices = self._fetch_segment_devices(segment)[:limit]

        attempted = sent = failed = 0
        for device in devices:
            attempted += 1
            if dry_run:
                continue
            status, provider_message_id, error_message = self._send_generic_fcm(
                title, body, device, data={"type": "broadcast", "segment": segment}
            )
            if status == "sent":
                sent += 1
            else:
                failed += 1
            self._log_generic_delivery(
                user_id=device.get("user_id"),
                device=device,
                status=status,
                title=title,
                body=body,
                kind="broadcast",
                provider_message_id=provider_message_id,
                error_message=error_message,
                extra={"segment": segment},
            )

        return TargetedPushSummary(
            success=failed == 0,
            segment=segment,
            audience_devices=len(devices),
            attempted_deliveries=attempted,
            sent_deliveries=sent,
            failed_deliveries=failed,
            dry_run=dry_run,
        )

    def dispatch_to_user(
        self,
        *,
        user_id: str,
        title: str,
        body: str,
        device_id: str | None = None,
        dry_run: bool = True,
    ) -> TargetedPushSummary:
        self._ensure_configured(require_firebase=not dry_run)
        title = title.strip()
        body = body.strip()
        if not title or not body:
            raise PushNotificationError("Message title and body are required.")
        if not user_id:
            raise PushNotificationError("A user is required.")
        devices = self._fetch_devices(user_id)
        if device_id:
            devices = [d for d in devices if str(d.get("id")) == str(device_id)]
            if not devices:
                raise PushNotificationError(
                    "That device is not enabled/registered for this user."
                )

        attempted = sent = failed = 0
        for device in devices:
            attempted += 1
            if dry_run:
                continue
            status, provider_message_id, error_message = self._send_generic_fcm(
                title, body, device, data={"type": "admin_direct"}
            )
            if status == "sent":
                sent += 1
            else:
                failed += 1
            self._log_generic_delivery(
                user_id=user_id,
                device=device,
                status=status,
                title=title,
                body=body,
                kind="admin_direct",
                provider_message_id=provider_message_id,
                error_message=error_message,
            )

        return TargetedPushSummary(
            success=failed == 0,
            segment="user",
            audience_devices=len(devices),
            attempted_deliveries=attempted,
            sent_deliveries=sent,
            failed_deliveries=failed,
            dry_run=dry_run,
        )

    def delivery_history(self, *, limit: int = 25) -> dict[str, Any]:
        self._ensure_configured(require_firebase=False)
        response = self._supabase_request(
            "GET",
            "/rest/v1/push_notification_deliveries",
            params={
                "select": "*",
                "order": "created_at.desc.nullslast,sent_at.desc.nullslast",
                "limit": str(limit),
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise PushNotificationError("Push delivery history response was invalid.")

        deliveries = [self._normalize_delivery(row) for row in rows]
        sent = sum(1 for row in deliveries if row["status"] == "sent")
        failed = sum(1 for row in deliveries if row["status"] == "failed")
        skipped = sum(1 for row in deliveries if row["status"] == "skipped")
        return {
            "source": "push_notification_deliveries",
            "count": len(deliveries),
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
            "deliveries": deliveries,
        }

    def _normalize_delivery(self, row: dict[str, Any]) -> dict[str, Any]:
        raw_json = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
        device = raw_json.get("device") if isinstance(raw_json.get("device"), dict) else {}
        kind = {
            "broadcast": "Broadcast",
            "admin_direct": "Admin direct message",
        }.get(raw_json.get("kind"), "Price-alert delivery")
        return {
            "id": row.get("id"),
            "kind": kind,
            "status": row.get("status") or "unknown",
            "createdAt": row.get("created_at") or row.get("sent_at"),
            "sentAt": row.get("sent_at"),
            "userId": row.get("user_id"),
            "priceAlertId": row.get("price_alert_id"),
            "portfolioItemId": row.get("portfolio_item_id"),
            "provider": row.get("provider") or "fcm",
            "platform": row.get("platform"),
            "title": row.get("title"),
            "body": row.get("body"),
            "errorMessage": row.get("error_message"),
            "providerMessageId": row.get("provider_message_id"),
            "deviceId": row.get("device_id") or device.get("id"),
            "deviceTokenId": row.get("device_id") or device.get("id"),
        }

    def disable_device_registration(self, device_id: str) -> dict[str, Any]:
        self._ensure_configured(require_firebase=False)
        device_id = device_id.strip()
        if not device_id:
            raise PushNotificationError("Device registration id is required.")
        response = self._supabase_request(
            "PATCH",
            "/rest/v1/push_device_registrations",
            params={"id": f"eq.{device_id}"},
            headers={"Prefer": "return=representation"},
            json={
                "enabled": False,
                "status": "disabled_by_admin",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        rows = response.json()
        if isinstance(rows, list) and rows:
            return {
                "deviceId": device_id,
                "disabled": True,
                "device": rows[0],
            }
        return {
            "deviceId": device_id,
            "disabled": False,
            "message": "No matching device registration was updated.",
        }

    def _ensure_configured(self, *, require_firebase: bool) -> None:
        if not self._supabase_url or not self._service_role_key:
            raise PushNotificationError("Supabase service role configuration is missing.")
        if require_firebase and (
            not self._firebase_project_id
            or (
                not self._firebase_access_token
                and not self._firebase_service_account_json
            )
        ):
            raise PushNotificationError("Firebase push configuration is missing.")

    def _mark_notified(self, alert: dict[str, Any]) -> None:
        """Flip a just-pushed alert triggered -> notified so it is not re-pushed."""
        iso = datetime.now(timezone.utc).isoformat()
        self._supabase_request(
            "PATCH",
            "/rest/v1/price_alerts",
            params={
                "id": f"eq.{alert['id']}",
                "user_id": f"eq.{alert['user_id']}",
            },
            headers={"Prefer": "return=minimal"},
            json={
                "status": "notified",
                "notified_at": iso,
                "updated_at": iso,
            },
        )

    def _fetch_triggered_alerts(self, *, limit: int) -> list[dict[str, Any]]:
        response = self._supabase_request(
            "GET",
            "/rest/v1/price_alerts",
            params={
                "select": "*",
                "enabled": "eq.true",
                "status": "eq.triggered",
                "order": "triggered_at.desc.nullslast",
                "limit": str(limit),
            },
        )
        data = response.json()
        return data if isinstance(data, list) else []

    def _fetch_devices(self, user_id: str) -> list[dict[str, Any]]:
        if not user_id:
            return []
        response = self._supabase_request(
            "GET",
            "/rest/v1/push_device_registrations",
            params={
                "select": "*",
                "user_id": f"eq.{user_id}",
                "enabled": "eq.true",
            },
        )
        data = response.json()
        return data if isinstance(data, list) else []

    def _fetch_all_enabled_devices(self) -> list[dict[str, Any]]:
        response = self._supabase_request(
            "GET",
            "/rest/v1/push_device_registrations",
            params={"select": "*", "enabled": "eq.true"},
        )
        data = response.json()
        return data if isinstance(data, list) else []

    def _fetch_pro_user_ids(self) -> set[str]:
        response = self._supabase_request(
            "GET",
            "/rest/v1/user_subscriptions",
            params={"select": "user_id", "plan": "eq.pro", "status": "eq.active"},
        )
        data = response.json()
        if not isinstance(data, list):
            return set()
        return {row.get("user_id") for row in data if row.get("user_id")}

    @staticmethod
    def _is_inactive_device(device: dict[str, Any], now: datetime) -> bool:
        last_seen_at = device.get("last_seen_at")
        if not last_seen_at:
            return True
        try:
            seen = datetime.fromisoformat(str(last_seen_at).replace("Z", "+00:00"))
        except ValueError:
            return True
        return (now - seen).days >= INACTIVE_DEVICE_DAYS

    def _fetch_segment_devices(self, segment: str) -> list[dict[str, Any]]:
        devices = self._fetch_all_enabled_devices()
        if segment == "all":
            return devices
        if segment == "pro":
            pro_user_ids = self._fetch_pro_user_ids()
            return [d for d in devices if d.get("user_id") in pro_user_ids]
        if segment == "inactive":
            now = datetime.now(timezone.utc)
            return [d for d in devices if self._is_inactive_device(d, now)]
        raise PushNotificationError(f"Unknown audience segment: {segment!r}")

    def _last_broadcast_today(self) -> dict[str, Any] | None:
        since = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()
        response = self._supabase_request(
            "GET",
            "/rest/v1/push_notification_deliveries",
            params={
                "select": "id,created_at",
                "raw_json->>kind": "eq.broadcast",
                "created_at": f"gte.{since}",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        rows = response.json()
        return rows[0] if isinstance(rows, list) and rows else None

    def _fetch_enabled_devices(
        self,
        *,
        user_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        params = {
            "select": "*",
            "enabled": "eq.true",
            "order": "updated_at.desc.nullslast,last_seen_at.desc.nullslast",
            "limit": str(limit),
        }
        if user_id:
            params["user_id"] = f"eq.{user_id}"
        response = self._supabase_request(
            "GET",
            "/rest/v1/push_device_registrations",
            params=params,
        )
        data = response.json()
        return data if isinstance(data, list) else []

    def _send_fcm(
        self,
        alert: dict[str, Any],
        device: dict[str, Any],
    ) -> tuple[str, str | None, str | None]:
        token = str(device.get("device_token") or "").strip()
        if not token:
            return "failed", None, "Device token is empty."

        title = "Price alert triggered"
        body = str(
            alert.get("message")
            or f"{alert.get('item_title', 'Collectible')} price alert triggered."
        )
        response = self._client.post(
            f"https://fcm.googleapis.com/v1/projects/{self._firebase_project_id}/messages:send",
            headers={
                "Authorization": f"Bearer {self._firebase_bearer_token()}",
                "Content-Type": "application/json",
            },
            json={
                "message": {
                    "token": token,
                    "notification": {"title": title, "body": body},
                    "data": {
                        "type": "price_alert",
                        "priceAlertId": str(alert.get("id") or ""),
                        "portfolioItemId": str(alert.get("portfolio_item_id") or ""),
                    },
                }
            },
        )
        if 200 <= response.status_code < 300:
            payload = response.json()
            return "sent", str(payload.get("name") or ""), None
        return "failed", None, response.text[:500]

    def _send_test_fcm(
        self,
        device: dict[str, Any],
    ) -> tuple[str, str | None, str | None]:
        token = str(device.get("device_token") or "").strip()
        if not token:
            return "failed", None, "Device token is empty."

        response = self._client.post(
            f"https://fcm.googleapis.com/v1/projects/{self._firebase_project_id}/messages:send",
            headers={
                "Authorization": f"Bearer {self._firebase_bearer_token()}",
                "Content-Type": "application/json",
            },
            json={
                "message": {
                    "token": token,
                    "notification": {
                        "title": "PackLox test push",
                        "body": "Your PackLox push notification setup is working.",
                    },
                    "data": {
                        "type": "test_push",
                        "source": "admin_test",
                    },
                }
            },
        )
        if 200 <= response.status_code < 300:
            payload = response.json()
            return "sent", str(payload.get("name") or ""), None
        return "failed", None, response.text[:500]

    def _send_generic_fcm(
        self,
        title: str,
        body: str,
        device: dict[str, Any],
        *,
        data: dict[str, str] | None = None,
    ) -> tuple[str, str | None, str | None]:
        token = str(device.get("device_token") or "").strip()
        if not token:
            return "failed", None, "Device token is empty."

        response = self._client.post(
            f"https://fcm.googleapis.com/v1/projects/{self._firebase_project_id}/messages:send",
            headers={
                "Authorization": f"Bearer {self._firebase_bearer_token()}",
                "Content-Type": "application/json",
            },
            json={
                "message": {
                    "token": token,
                    "notification": {"title": title, "body": body},
                    "data": data or {},
                }
            },
        )
        if 200 <= response.status_code < 300:
            payload = response.json()
            return "sent", str(payload.get("name") or ""), None
        return "failed", None, response.text[:500]

    def _log_generic_delivery(
        self,
        *,
        user_id: str | None,
        device: dict[str, Any] | None,
        status: str,
        title: str,
        body: str,
        kind: str,
        provider_message_id: str | None = None,
        error_message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._supabase_request(
            "POST",
            "/rest/v1/push_notification_deliveries",
            headers={"Prefer": "return=minimal"},
            json=[
                {
                    "user_id": user_id,
                    "price_alert_id": None,
                    "portfolio_item_id": None,
                    "provider": (device or {}).get("provider") or "fcm",
                    "platform": (device or {}).get("platform"),
                    "title": title,
                    "body": body,
                    "status": status,
                    "provider_message_id": provider_message_id,
                    "error_message": error_message,
                    "sent_at": datetime.now(timezone.utc).isoformat()
                    if status == "sent"
                    else None,
                    "raw_json": {"kind": kind, "device": device, **(extra or {})},
                }
            ],
        )

    def _firebase_bearer_token(self) -> str:
        if self._firebase_access_token:
            return self._firebase_access_token
        try:
            service_account_info = json.loads(self._firebase_service_account_json)
            credentials = service_account.Credentials.from_service_account_info(
                service_account_info,
                scopes=["https://www.googleapis.com/auth/firebase.messaging"],
            )
            credentials.refresh(Request())
            token = credentials.token
            if not token:
                raise PushNotificationError(
                    "Firebase service account did not return an access token."
                )
            return token
        except PushNotificationError:
            raise
        except Exception as error:
            raise PushNotificationError(
                "Firebase service account configuration is invalid."
            ) from error

    def _log_delivery(
        self,
        alert: dict[str, Any],
        device: dict[str, Any] | None,
        status: str,
        *,
        provider_message_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._supabase_request(
            "POST",
            "/rest/v1/push_notification_deliveries",
            headers={"Prefer": "return=minimal"},
            json=[
                {
                    "user_id": alert.get("user_id"),
                    "price_alert_id": alert.get("id"),
                    "portfolio_item_id": alert.get("portfolio_item_id"),
                    "provider": (device or {}).get("provider") or "fcm",
                    "platform": (device or {}).get("platform"),
                    "title": "Price alert triggered",
                    "body": str(alert.get("message") or "Price alert triggered."),
                    "status": status,
                    "provider_message_id": provider_message_id,
                    "error_message": error_message,
                    "sent_at": datetime.now(timezone.utc).isoformat()
                    if status == "sent"
                    else None,
                    "raw_json": {"alert": alert, "device": device},
                }
            ],
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
