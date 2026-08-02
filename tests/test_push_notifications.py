import unittest

import httpx

from app.services.push.price_alert_push_service import PriceAlertPushService


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
        if "push_device_registrations" in url:
            return _response(
                [
                    {
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
