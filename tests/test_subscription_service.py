import time
import unittest
from unittest.mock import Mock

import httpx

from app.services.subscription.apple_verification_service import (
    AppleEntitlement,
    AppleTransactionInvalidError,
)
from app.services.subscription.google_play_verification_service import (
    GooglePlayEntitlement,
    GooglePlayPurchaseInvalidError,
)
from app.services.subscription.subscription_service import (
    SubscriptionPurchaseInvalidError,
    SubscriptionService,
)


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


class VerifyAndGrantRealVerificationTest(unittest.TestCase):
    def test_google_play_grants_the_verified_plan_not_the_claimed_one(self) -> None:
        # The client claims "premium" but Google's own record says "pro" --
        # the granted plan must come from the verified result, not the
        # client's claim, or a client could self-report any plan it wants.
        client = _FakeSubscriptionClient()
        google_verifier = Mock()
        google_verifier.verify_purchase_token.return_value = GooglePlayEntitlement(
            plan="pro", is_active=True, subscription_state="SUBSCRIPTION_STATE_ACTIVE",
            product_id="collectiq_pro_monthly_test", expires_at="2026-09-16T00:00:00Z", order_id="order-1",
        )
        service = SubscriptionService(
            supabase_url="https://supabase.test", service_role_key="service-role", anon_key="anon-key",
            client=client, google_play_verifier=google_verifier,
        )

        result = service.verify_and_grant(
            user_id="user-1", plan="premium", source="google_play", purchase_token="real-token",
        )

        self.assertEqual(result["plan"], "pro")
        google_verifier.verify_purchase_token.assert_called_once_with("real-token")
        row = client.requests[-1]["json"][0]
        self.assertEqual(row["purchase_token"], "real-token")
        self.assertEqual(row["product_id"], "collectiq_pro_monthly_test")

    def test_google_play_grace_period_status_is_recorded(self) -> None:
        client = _FakeSubscriptionClient()
        google_verifier = Mock()
        google_verifier.verify_purchase_token.return_value = GooglePlayEntitlement(
            plan="pro", is_active=True, subscription_state="SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
            product_id="collectiq_pro_monthly_test", expires_at="2026-08-01T00:00:00Z", order_id=None,
        )
        service = SubscriptionService(
            supabase_url="https://supabase.test", service_role_key="service-role", anon_key="anon-key",
            client=client, google_play_verifier=google_verifier,
        )

        service.verify_and_grant(user_id="user-1", plan="pro", source="google_play", purchase_token="tok")

        row = client.requests[-1]["json"][0]
        self.assertEqual(row["status"], "in_grace")

    def test_google_play_invalid_token_raises_purchase_invalid_and_writes_nothing(self) -> None:
        client = _FakeSubscriptionClient()
        google_verifier = Mock()
        google_verifier.verify_purchase_token.side_effect = GooglePlayPurchaseInvalidError("nope")
        service = SubscriptionService(
            supabase_url="https://supabase.test", service_role_key="service-role", anon_key="anon-key",
            client=client, google_play_verifier=google_verifier,
        )

        with self.assertRaises(SubscriptionPurchaseInvalidError):
            service.verify_and_grant(user_id="user-1", plan="pro", source="google_play", purchase_token="forged")

        self.assertFalse(any(r["url"].endswith("/rest/v1/user_subscriptions") for r in client.requests))

    def test_apple_grants_the_verified_plan_not_the_claimed_one(self) -> None:
        client = _FakeSubscriptionClient()
        apple_verifier = Mock()
        future_ms = int((time.time() + 3600) * 1000)
        apple_verifier.verify_signed_transaction.return_value = AppleEntitlement(
            plan="pro", is_active=True, product_id="collectiq_pro_monthly_test",
            expires_at=str(future_ms), original_transaction_id="orig-txn-1", revoked=False,
        )
        service = SubscriptionService(
            supabase_url="https://supabase.test", service_role_key="service-role", anon_key="anon-key",
            client=client, apple_verifier=apple_verifier,
        )

        result = service.verify_and_grant(
            user_id="user-1", plan="premium", source="app_store", purchase_token="signed-jws",
        )

        self.assertEqual(result["plan"], "pro")
        apple_verifier.verify_signed_transaction.assert_called_once_with("signed-jws")
        row = client.requests[-1]["json"][0]
        self.assertEqual(row["original_transaction_id"], "orig-txn-1")
        self.assertIsNotNone(row["current_period_end"])

    def test_apple_revoked_transaction_is_recorded_as_canceled(self) -> None:
        client = _FakeSubscriptionClient()
        apple_verifier = Mock()
        future_ms = int((time.time() + 3600) * 1000)
        apple_verifier.verify_signed_transaction.return_value = AppleEntitlement(
            plan="pro", is_active=False, product_id="collectiq_pro_monthly_test",
            expires_at=str(future_ms), original_transaction_id="orig-txn-2", revoked=True,
        )
        service = SubscriptionService(
            supabase_url="https://supabase.test", service_role_key="service-role", anon_key="anon-key",
            client=client, apple_verifier=apple_verifier,
        )

        service.verify_and_grant(user_id="user-1", plan="pro", source="app_store", purchase_token="signed-jws")

        row = client.requests[-1]["json"][0]
        self.assertEqual(row["status"], "canceled")
        self.assertEqual(row["plan"], "free")

    def test_apple_invalid_transaction_raises_purchase_invalid(self) -> None:
        client = _FakeSubscriptionClient()
        apple_verifier = Mock()
        apple_verifier.verify_signed_transaction.side_effect = AppleTransactionInvalidError("nope")
        service = SubscriptionService(
            supabase_url="https://supabase.test", service_role_key="service-role", anon_key="anon-key",
            client=client, apple_verifier=apple_verifier,
        )

        with self.assertRaises(SubscriptionPurchaseInvalidError):
            service.verify_and_grant(user_id="user-1", plan="pro", source="app_store", purchase_token="forged")


class ResyncFromWebhookTest(unittest.TestCase):
    def test_resync_from_google_play_token_looks_up_user_and_regrants(self) -> None:
        client = _FakeSubscriptionClient()
        client.subscription_lookup_rows = [{"user_id": "user-42"}]
        google_verifier = Mock()
        google_verifier.verify_purchase_token.return_value = GooglePlayEntitlement(
            plan="pro", is_active=True, subscription_state="SUBSCRIPTION_STATE_ACTIVE",
            product_id="collectiq_pro_monthly_test", expires_at="2026-09-16T00:00:00Z", order_id="order-1",
        )
        service = SubscriptionService(
            supabase_url="https://supabase.test", service_role_key="service-role", anon_key="anon-key",
            client=client, google_play_verifier=google_verifier,
        )

        result = service.resync_from_google_play_token("real-token")

        self.assertIsNotNone(result)
        self.assertEqual(result["plan"], "pro")
        lookup_request = next(
            r for r in client.requests
            if r["method"] == "GET" and r["url"].endswith("/rest/v1/user_subscriptions")
        )
        self.assertEqual(lookup_request["params"]["purchase_token"], "eq.real-token")

    def test_resync_from_google_play_token_returns_none_when_no_row_matches(self) -> None:
        client = _FakeSubscriptionClient()
        client.subscription_lookup_rows = []
        service = SubscriptionService(
            supabase_url="https://supabase.test", service_role_key="service-role", anon_key="anon-key",
            client=client,
        )

        result = service.resync_from_google_play_token("unknown-token")

        self.assertIsNone(result)
        self.assertFalse(any(
            r["method"] == "POST" and r["url"].endswith("/rest/v1/user_subscriptions") for r in client.requests
        ))

    def test_resync_from_apple_original_transaction_id_looks_up_user_and_regrants(self) -> None:
        client = _FakeSubscriptionClient()
        client.subscription_lookup_rows = [{"user_id": "user-42"}]
        apple_verifier = Mock()
        future_ms = int((time.time() + 3600) * 1000)
        apple_verifier.verify_signed_transaction.return_value = AppleEntitlement(
            plan="pro", is_active=True, product_id="collectiq_pro_monthly_test",
            expires_at=str(future_ms), original_transaction_id="orig-txn-1", revoked=False,
        )
        service = SubscriptionService(
            supabase_url="https://supabase.test", service_role_key="service-role", anon_key="anon-key",
            client=client, apple_verifier=apple_verifier,
        )

        result = service.resync_from_apple_original_transaction_id("orig-txn-1", signed_transaction="signed-jws")

        self.assertIsNotNone(result)
        lookup_request = next(
            r for r in client.requests
            if r["method"] == "GET" and r["url"].endswith("/rest/v1/user_subscriptions")
        )
        self.assertEqual(lookup_request["params"]["original_transaction_id"], "eq.orig-txn-1")


class _FakeSubscriptionClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.scan_usage_rows: list[dict] = []
        self.subscription_lookup_rows: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if url.endswith("/rest/v1/user_scan_usage") and method == "GET":
            return _response(self.scan_usage_rows)
        if url.endswith("/rest/v1/user_scan_usage") and method == "PATCH":
            return _response(None)
        if url.endswith("/rest/v1/user_subscriptions") and method == "GET":
            return _response(self.subscription_lookup_rows)
        if url.endswith("/rest/v1/user_subscriptions") and method == "POST":
            row = dict(kwargs["json"][0])
            return _response([{"current_period_end": None, **row}])
        raise AssertionError(f"Unexpected request: {method} {url}")


def _response(payload) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("GET", "https://supabase.test"),
    )


if __name__ == "__main__":
    unittest.main()
