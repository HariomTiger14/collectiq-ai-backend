import unittest
from datetime import datetime, timedelta, timezone

import httpx

from app.services.push.price_alert_push_service import (
    PriceAlertPushService,
    PushNotificationError,
)


class PushNotificationServiceTest(unittest.TestCase):
    def test_dry_run_counts_triggered_alerts_and_devices_without_sending(self) -> None:
        client = _FakePushHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="",
            client=client,
        )

        summary = service.dispatch_triggered_alerts(dry_run=True)

        self.assertTrue(summary.success)
        self.assertEqual(summary.scanned_alerts, 1)
        self.assertEqual(summary.attempted_deliveries, 1)
        self.assertEqual(summary.skipped_deliveries, 1)
        self.assertEqual(client.fcm_posts, 0)

    def test_send_posts_to_fcm_and_logs_delivery(self) -> None:
        client = _FakePushHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        summary = service.dispatch_triggered_alerts(dry_run=False)

        self.assertTrue(summary.success)
        self.assertEqual(summary.sent_deliveries, 1)
        self.assertEqual(client.fcm_posts, 1)
        delivery_logs = [
            request
            for request in client.requests
            if "push_notification_deliveries" in request["url"]
        ]
        self.assertEqual(len(delivery_logs), 1)
        self.assertEqual(delivery_logs[0]["json"][0]["status"], "sent")

    def test_sent_alert_is_marked_notified(self) -> None:
        client = _FakePushHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        service.dispatch_triggered_alerts(dry_run=False)

        notified = [
            request
            for request in client.requests
            if request["method"] == "PATCH" and "price_alerts" in request["url"]
        ]
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0]["json"]["status"], "notified")
        self.assertIn("notified_at", notified[0]["json"])

    def test_dry_run_does_not_mark_notified(self) -> None:
        client = _FakePushHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="",
            client=client,
        )

        service.dispatch_triggered_alerts(dry_run=True)

        notified = [
            request
            for request in client.requests
            if request["method"] == "PATCH" and "price_alerts" in request["url"]
        ]
        self.assertEqual(notified, [])

    def test_test_notification_sends_to_registered_devices(self) -> None:
        client = _FakePushHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        summary = service.dispatch_test_notification(dry_run=False)

        self.assertTrue(summary.success)
        self.assertEqual(summary.scanned_alerts, 0)
        self.assertEqual(summary.attempted_deliveries, 1)
        self.assertEqual(summary.sent_deliveries, 1)
        self.assertEqual(client.fcm_posts, 1)
        fcm_request = [
            request
            for request in client.requests
            if "fcm.googleapis.com" in request["url"]
        ][0]
        self.assertEqual(
            fcm_request["json"]["message"]["data"]["type"],
            "test_push",
        )

    def test_test_price_alert_notification_sends_portfolio_route_data(self) -> None:
        client = _FakePushHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        summary = service.dispatch_test_price_alert_notification(
            portfolio_item_id="item-123",
            dry_run=False,
        )

        self.assertTrue(summary.success)
        self.assertEqual(summary.attempted_deliveries, 1)
        self.assertEqual(summary.sent_deliveries, 1)
        fcm_request = [
            request
            for request in client.requests
            if "fcm.googleapis.com" in request["url"]
        ][0]
        self.assertEqual(
            fcm_request["json"]["message"]["data"],
            {
                "type": "price_alert",
                "priceAlertId": "admin-test-price-alert",
                "portfolioItemId": "item-123",
            },
        )

    def test_delivery_history_reads_recent_delivery_rows(self) -> None:
        client = _FakePushHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="",
            client=client,
        )

        history = service.delivery_history(limit=10)

        self.assertEqual(history["source"], "push_notification_deliveries")
        self.assertEqual(history["count"], 2)
        self.assertEqual(history["sent"], 1)
        self.assertEqual(history["failed"], 1)
        self.assertEqual(history["deliveries"][0]["portfolioItemId"], "item-1")
        self.assertEqual(history["deliveries"][1]["deviceId"], "device-2")

    def test_disable_device_registration_marks_token_disabled(self) -> None:
        client = _FakePushHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="",
            client=client,
        )

        result = service.disable_device_registration("device-2")

        self.assertTrue(result["disabled"])
        patch_request = [
            request
            for request in client.requests
            if request["method"] == "PATCH" and "push_device_registrations" in request["url"]
        ][0]
        self.assertEqual(patch_request["params"]["id"], "eq.device-2")
        self.assertFalse(patch_request["json"]["enabled"])
        self.assertEqual(patch_request["json"]["status"], "disabled_by_admin")


class AudienceAndBroadcastTest(unittest.TestCase):
    def test_audience_counts_splits_by_pro_and_inactivity(self) -> None:
        client = _FakeBroadcastHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        counts = service.audience_counts()

        self.assertEqual(counts["all"], 3)
        self.assertEqual(counts["pro"], 1)
        self.assertEqual(counts["inactive"], 1)
        self.assertEqual(counts["inactiveDays"], 30)

    def test_broadcast_dry_run_counts_audience_without_sending(self) -> None:
        client = _FakeBroadcastHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        summary = service.dispatch_broadcast(
            segment="all", title="Hello", body="World", dry_run=True,
        )

        self.assertTrue(summary.success)
        self.assertEqual(summary.audience_devices, 3)
        self.assertEqual(summary.attempted_deliveries, 3)
        self.assertEqual(client.fcm_posts, 0)

    def test_broadcast_live_send_only_targets_the_pro_segment(self) -> None:
        client = _FakeBroadcastHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        summary = service.dispatch_broadcast(
            segment="pro", title="Hello Pro", body="World", dry_run=False,
        )

        self.assertTrue(summary.success)
        self.assertEqual(summary.audience_devices, 1)
        self.assertEqual(summary.sent_deliveries, 1)
        self.assertEqual(client.fcm_posts, 1)
        delivery_logs = [
            r for r in client.requests
            if r["method"] == "POST" and "push_notification_deliveries" in r["url"]
        ]
        self.assertEqual(len(delivery_logs), 1)
        self.assertEqual(delivery_logs[0]["json"][0]["raw_json"]["segment"], "pro")

    def test_broadcast_live_send_blocked_by_daily_rate_limit(self) -> None:
        client = _FakeBroadcastHttpClient(existing_broadcast_today=True)
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        with self.assertRaises(PushNotificationError):
            service.dispatch_broadcast(
                segment="all", title="Hello", body="World", dry_run=False,
            )
        self.assertEqual(client.fcm_posts, 0)

    def test_broadcast_dry_run_is_never_rate_limited(self) -> None:
        client = _FakeBroadcastHttpClient(existing_broadcast_today=True)
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        summary = service.dispatch_broadcast(
            segment="all", title="Hello", body="World", dry_run=True,
        )

        self.assertEqual(summary.attempted_deliveries, 3)

    def test_dispatch_to_user_sends_to_all_of_that_users_devices(self) -> None:
        client = _FakeBroadcastHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        summary = service.dispatch_to_user(
            user_id="user-multi", title="Hi", body="There", dry_run=False,
        )

        self.assertTrue(summary.success)
        self.assertEqual(summary.audience_devices, 2)
        self.assertEqual(summary.sent_deliveries, 2)

    def test_dispatch_to_user_can_target_a_single_device(self) -> None:
        client = _FakeBroadcastHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        summary = service.dispatch_to_user(
            user_id="user-multi",
            title="Hi",
            body="There",
            device_id="device-multi-2",
            dry_run=False,
        )

        self.assertEqual(summary.audience_devices, 1)
        self.assertEqual(summary.sent_deliveries, 1)

    def test_dispatch_to_user_rejects_a_device_not_owned_by_that_user(self) -> None:
        client = _FakeBroadcastHttpClient()
        service = PriceAlertPushService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            firebase_project_id="packlox-test",
            firebase_access_token="firebase-access",
            client=client,
        )

        with self.assertRaises(PushNotificationError):
            service.dispatch_to_user(
                user_id="user-multi",
                title="Hi",
                body="There",
                device_id="device-not-owned",
                dry_run=False,
            )


class _FakeBroadcastHttpClient:
    def __init__(self, *, existing_broadcast_today: bool = False) -> None:
        self.requests: list[dict] = []
        self.fcm_posts = 0
        self._existing_broadcast_today = existing_broadcast_today

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        params = kwargs.get("params") or {}

        if "user_subscriptions" in url:
            return _response([{"user_id": "user-pro"}])

        if "push_device_registrations" in url:
            user_filter = params.get("user_id")
            if user_filter == "eq.user-multi":
                return _response(
                    [
                        {
                            "id": "device-multi-1",
                            "user_id": "user-multi",
                            "device_token": "token-multi-1",
                            "provider": "fcm",
                            "platform": "ios",
                        },
                        {
                            "id": "device-multi-2",
                            "user_id": "user-multi",
                            "device_token": "token-multi-2",
                            "provider": "fcm",
                            "platform": "android",
                        },
                    ]
                )
            # All enabled devices across the whole audience (no user filter).
            now = datetime.now(timezone.utc)
            recent = (now - timedelta(days=5)).isoformat()
            stale = (now - timedelta(days=45)).isoformat()
            return _response(
                [
                    {
                        "id": "device-free",
                        "user_id": "user-free",
                        "device_token": "token-free",
                        "provider": "fcm",
                        "platform": "android",
                        "last_seen_at": recent,
                    },
                    {
                        "id": "device-pro",
                        "user_id": "user-pro",
                        "device_token": "token-pro",
                        "provider": "fcm",
                        "platform": "ios",
                        "last_seen_at": recent,
                    },
                    {
                        "id": "device-stale",
                        "user_id": "user-stale",
                        "device_token": "token-stale",
                        "provider": "fcm",
                        "platform": "ios",
                        "last_seen_at": stale,
                    },
                ]
            )

        if "push_notification_deliveries" in url and method == "GET":
            if self._existing_broadcast_today:
                return _response(
                    [{"id": "broadcast-1", "created_at": "2026-08-16T01:00:00Z"}]
                )
            return _response([])

        if "push_notification_deliveries" in url:
            return _response({})

        raise AssertionError(f"Unexpected request URL: {url}")

    def post(self, url: str, **kwargs):
        self.fcm_posts += 1
        self.requests.append({"method": "POST", "url": url, **kwargs})
        return _response({"name": "projects/packlox-test/messages/message-1"})


class _FakePushHttpClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.fcm_posts = 0

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if "price_alerts" in url:
            return _response(
                [
                    {
                        "id": "alert-1",
                        "user_id": "user-1",
                        "portfolio_item_id": "item-1",
                        "item_title": "Charizard",
                        "message": "Charizard rose above USD 200.",
                    }
                ]
            )
        if "push_device_registrations" in url and method == "PATCH":
            return _response(
                [
                    {
                        "id": kwargs["params"]["id"].removeprefix("eq."),
                        "enabled": False,
                        "status": "disabled_by_admin",
                    }
                ]
            )
        if "push_device_registrations" in url:
            return _response(
                [
                    {
                        "id": "device-1",
                        "user_id": "user-1",
                        "device_token": "device-token-1",
                        "provider": "fcm",
                        "platform": "ios",
                    }
                ]
            )
        if "push_notification_deliveries" in url and method == "GET":
            return _response(
                [
                    {
                        "id": "delivery-1",
                        "user_id": "user-1",
                        "price_alert_id": "alert-1",
                        "portfolio_item_id": "item-1",
                        "provider": "fcm",
                        "platform": "ios",
                        "title": "Price alert triggered",
                        "body": "Charizard rose above USD 200.",
                        "status": "sent",
                        "provider_message_id": "message-1",
                        "sent_at": "2026-08-02T01:00:00Z",
                        "created_at": "2026-08-02T01:00:00Z",
                    },
                    {
                        "id": "delivery-2",
                        "user_id": "user-2",
                        "price_alert_id": "alert-2",
                        "portfolio_item_id": "item-2",
                        "provider": "fcm",
                        "platform": "android",
                        "title": "Price alert triggered",
                        "body": "Delivery failed.",
                        "status": "failed",
                        "error_message": "FCM rejected token.",
                        "created_at": "2026-08-02T01:05:00Z",
                        "raw_json": {"device": {"id": "device-2"}},
                    },
                ]
            )
        if "push_notification_deliveries" in url:
            return _response({})
        raise AssertionError(f"Unexpected request URL: {url}")

    def post(self, url: str, **kwargs):
        self.fcm_posts += 1
        self.requests.append({"method": "POST", "url": url, **kwargs})
        return _response({"name": "projects/packlox-test/messages/message-1"})


def _response(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=payload,
        request=httpx.Request("GET", "https://example.test"),
    )


if __name__ == "__main__":
    unittest.main()
