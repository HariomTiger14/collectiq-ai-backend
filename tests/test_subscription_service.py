import unittest

import httpx

from app.services.subscription.subscription_service import SubscriptionService


class SubscriptionServiceScanUsageTest(unittest.TestCase):
    def test_get_scan_usage_sums_current_month_rows(self) -> None:
        client = _FakeSubscriptionClient()
        client.scan_usage_rows = [{"scans_used": 3}, {"scans_used": 2}]
        service = SubscriptionService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            anon_key="anon-key",
            client=client,
        )

        result = service.get_scan_usage("user-1", free_monthly_limit=30)

        self.assertEqual(result["used"], 5)
        self.assertEqual(result["limit"], 30)
        self.assertTrue(result["periodStart"].endswith("-01"))
        request = client.requests[-1]
        self.assertTrue(request["url"].endswith("/rest/v1/user_scan_usage"))
        self.assertEqual(request["params"]["user_id"], "eq.user-1")

    def test_get_scan_usage_treats_missing_rows_as_zero(self) -> None:
        client = _FakeSubscriptionClient()
        client.scan_usage_rows = []
        service = SubscriptionService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            anon_key="anon-key",
            client=client,
        )

        result = service.get_scan_usage("user-1", free_monthly_limit=30)

        self.assertEqual(result["used"], 0)

    def test_reset_scan_usage_patches_current_month_rows_to_zero(self) -> None:
        client = _FakeSubscriptionClient()
        service = SubscriptionService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            anon_key="anon-key",
            client=client,
        )

        result = service.reset_scan_usage("user-1")

        self.assertEqual(result["userId"], "user-1")
        self.assertEqual(result["used"], 0)
        request = client.requests[-1]
        self.assertEqual(request["method"], "PATCH")
        self.assertTrue(request["url"].endswith("/rest/v1/user_scan_usage"))
        self.assertEqual(request["json"]["scans_used"], 0)

    def test_admin_override_is_a_valid_subscription_source(self) -> None:
        client = _FakeSubscriptionClient()
        service = SubscriptionService(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            anon_key="anon-key",
            client=client,
        )

        result = service.verify_and_grant(
            user_id="user-1", plan="pro", source="admin_override", purchase_token=None
        )

        self.assertEqual(result["plan"], "pro")
        self.assertEqual(result["source"], "admin_override")
        request = client.requests[-1]
        self.assertEqual(request["json"][0]["source"], "admin_override")


class _FakeSubscriptionClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.scan_usage_rows: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if url.endswith("/rest/v1/user_scan_usage") and method == "GET":
            return _response(self.scan_usage_rows)
        if url.endswith("/rest/v1/user_scan_usage") and method == "PATCH":
            return _response(None)
        if url.endswith("/rest/v1/user_subscriptions") and method == "POST":
            row = dict(kwargs["json"][0])
            return _response([{**row, "current_period_end": None}])
        raise AssertionError(f"Unexpected request: {method} {url}")


def _response(payload) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("GET", "https://supabase.test"),
    )


if __name__ == "__main__":
    unittest.main()
