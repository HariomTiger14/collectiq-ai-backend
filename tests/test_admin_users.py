import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services.admin_audit_service import clear_in_memory_audit_events
from app.services.admin_user_service import AdminUserService, SupabaseAdminUserRepository


class AdminUsersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        clear_in_memory_audit_events()

    def tearDown(self) -> None:
        clear_in_memory_audit_events()

    def test_admin_users_requires_admin_token(self) -> None:
        response = self.client.get("/admin/users")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_import_not_configured")

    def test_repository_lists_users_with_counts(self) -> None:
        client = _FakeAdminUsersClient()
        service = AdminUserService(
            repository=SupabaseAdminUserRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.list_users(query="collector@example.com", limit=10)

        self.assertEqual(payload["count"], 1)
        user = payload["users"][0]
        self.assertEqual(user["email"], "collector@example.com")
        self.assertEqual(user["authStatus"], "confirmed")
        self.assertEqual(user["displayName"], "Collector One")
        self.assertEqual(user["portfolioCount"], 2)
        self.assertEqual(user["scanCount"], 1)
        self.assertEqual(user["pushDeviceCount"], 1)
        self.assertEqual(user["plan"], "pro")
        self.assertEqual(user["planStatus"], "active")
        # Console access (role/isAdmin) is independent of subscription plan —
        # this user is Pro *and* an admin; both must be reported.
        self.assertEqual(user["role"], "admin")
        self.assertTrue(user["isAdmin"])
        self.assertEqual(client.requests[0]["headers"]["Authorization"], "Bearer service-role")
        self.assertEqual(client.requests[0]["params"]["email"], "collector@example.com")

    def test_repository_lists_users_default_role_when_profile_has_none(self) -> None:
        client = _FakeAdminUsersClientNoRole()
        service = AdminUserService(
            repository=SupabaseAdminUserRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.list_users(query=None, limit=10)

        user = payload["users"][0]
        self.assertEqual(user["role"], "user")
        self.assertFalse(user["isAdmin"])

    def test_repository_gets_user_detail_with_recent_activity(self) -> None:
        client = _FakeAdminUsersClient()
        subscription_service = Mock()
        subscription_service.get_entitlement.return_value = {
            "plan": "pro",
            "status": "active",
            "source": "google_play",
            "currentPeriodEnd": None,
        }
        subscription_service.get_scan_usage.return_value = {
            "used": 5,
            "limit": 30,
            "periodStart": "2026-07-01",
        }
        service = AdminUserService(
            repository=SupabaseAdminUserRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            ),
            subscription_service=subscription_service,
        )

        payload = service.get_user_detail("user-1")

        self.assertTrue(payload["success"])
        user = payload["user"]
        self.assertEqual(user["id"], "user-1")
        self.assertEqual(user["portfolioValue"], 125.5)
        self.assertEqual(len(user["recentPortfolioItems"]), 2)
        self.assertEqual(len(user["recentScans"]), 1)
        self.assertEqual([item["id"] for item in user["pricingReviewItems"]], ["item-2"])
        self.assertEqual(user["subscription"]["plan"], "pro")
        self.assertEqual(user["scanUsage"]["used"], 5)
        self.assertEqual(len(user["priceAlerts"]), 1)
        self.assertEqual(user["priceAlerts"][0]["itemTitle"], "Charizard")
        self.assertEqual(len(user["pushDevices"]), 1)
        self.assertEqual(user["pushDevices"][0]["platform"], "ios")

    def test_override_subscription_delegates_to_subscription_service(self) -> None:
        subscription_service = Mock()
        subscription_service.verify_and_grant.return_value = {
            "plan": "pro",
            "status": "active",
            "source": "admin_override",
            "currentPeriodEnd": None,
        }
        client = _FakeAdminUsersClient()
        service = AdminUserService(
            repository=SupabaseAdminUserRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            ),
            subscription_service=subscription_service,
        )

        payload = service.override_subscription(user_id="user-1", plan="pro")

        self.assertTrue(payload["success"])
        self.assertEqual(payload["subscription"]["plan"], "pro")
        subscription_service.verify_and_grant.assert_called_once_with(
            user_id="user-1", plan="pro", source="admin_override", purchase_token=None
        )

    def test_reset_scan_usage_delegates_to_subscription_service(self) -> None:
        subscription_service = Mock()
        subscription_service.reset_scan_usage.return_value = {
            "userId": "user-1",
            "used": 0,
            "periodStart": "2026-07-01",
        }
        client = _FakeAdminUsersClient()
        service = AdminUserService(
            repository=SupabaseAdminUserRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            ),
            subscription_service=subscription_service,
        )

        payload = service.reset_scan_usage("user-1")

        self.assertTrue(payload["success"])
        self.assertEqual(payload["scanUsage"]["used"], 0)
        subscription_service.reset_scan_usage.assert_called_once_with("user-1")

    def test_admin_user_detail_endpoint_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.get_user_detail.return_value = {
                "success": True,
                "user": {"id": "user-1", "email": "collector@example.com"},
            }
            response = self.client.get(
                "/admin/users/user-1",
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_users.detail_viewed",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["id"], "user-1")
        event = audit_response.json()["events"][0]
        self.assertEqual(event["action"], "admin_users.detail_viewed")
        self.assertEqual(event["metadata"]["userId"], "user-1")

    def test_subscription_override_endpoint_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.override_subscription.return_value = {
                "success": True,
                "userId": "user-1",
                "subscription": {"plan": "pro", "status": "active", "source": "admin_override"},
            }
            response = self.client.post(
                "/admin/users/user-1/subscription",
                json={"plan": "pro"},
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_users.subscription_overridden",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["subscription"]["plan"], "pro")
        service.return_value.override_subscription.assert_called_once_with(
            user_id="user-1", plan="pro"
        )
        event = audit_response.json()["events"][0]
        self.assertEqual(event["metadata"]["plan"], "pro")

    def test_subscription_override_rejects_invalid_plan(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings:
            auth_settings.admin_import_token = "secret-token"
            response = self.client.post(
                "/admin/users/user-1/subscription",
                json={"plan": "diamond"},
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 422)

    def test_scan_usage_reset_endpoint_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.reset_scan_usage.return_value = {
                "success": True,
                "scanUsage": {"used": 0, "limit": 30, "periodStart": "2026-07-01"},
            }
            response = self.client.post(
                "/admin/users/user-1/scan-usage/reset",
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_users.scan_usage_reset",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scanUsage"]["used"], 0)
        service.return_value.reset_scan_usage.assert_called_once_with("user-1")
        event = audit_response.json()["events"][0]
        self.assertEqual(event["targetId"], "user-1")

    def test_admin_users_endpoint_records_search_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.list_users.return_value = {
                "success": True,
                "query": "collector@example.com",
                "count": 1,
                "users": [{"id": "user-1", "email": "collector@example.com"}],
            }
            response = self.client.get(
                "/admin/users?q=collector@example.com",
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_users.searched",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        event = audit_response.json()["events"][0]
        self.assertEqual(event["action"], "admin_users.searched")
        self.assertEqual(event["metadata"]["query"], "collector@example.com")


class _FakeAdminUsersClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if url.endswith("/auth/v1/admin/users/user-1"):
            return _response(
                {
                    "id": "user-1",
                    "email": "collector@example.com",
                    "created_at": "2026-07-01T00:00:00Z",
                    "email_confirmed_at": "2026-07-01T00:00:00Z",
                    "last_sign_in_at": "2026-07-29T00:00:00Z",
                }
            )
        if url.endswith("/auth/v1/admin/users"):
            return _response(
                {
                    "users": [
                        {
                            "id": "user-1",
                            "email": "collector@example.com",
                            "created_at": "2026-07-01T00:00:00Z",
                            "email_confirmed_at": "2026-07-01T00:00:00Z",
                            "last_sign_in_at": "2026-07-29T00:00:00Z",
                        }
                    ]
                }
            )
        if url.endswith("/rest/v1/profiles"):
            return _response(
                [
                    {
                        "id": "user-1",
                        "display_name": "Collector One",
                        "role": "admin",
                        "is_admin": True,
                        "updated_at": "2026-07-29T01:00:00Z",
                    }
                ]
            )
        if url.endswith("/rest/v1/portfolio_items"):
            return _response([
                {
                    "id": "item-1",
                    "title": "Healthy Card",
                    "category": "Card",
                    "pricing": {
                        "estimatedMarketValue": 125.5,
                        "currency": "USD",
                        "pricingConfidence": 90,
                    },
                },
                {
                    "id": "item-2",
                    "title": "Needs Review",
                    "category": "Card",
                    "needs_review": True,
                    "pricing": {
                        "estimatedMarketValue": 0,
                        "currency": "USD",
                        "pricingConfidence": 20,
                    },
                },
            ])
        if url.endswith("/rest/v1/scan_analysis_events"):
            return _response([{"id": "scan-1", "status": "failed", "provider": "openai"}])
        if url.endswith("/rest/v1/price_alerts"):
            return _response(
                [
                    {
                        "id": "alert-1",
                        "item_title": "Charizard",
                        "portfolio_item_id": "item-1",
                        "rule_type": "priceRisesAboveAmount",
                        "target_amount": 500,
                        "enabled": True,
                        "status": "active",
                        "updated_at": "2026-07-29T00:00:00Z",
                    }
                ]
            )
        if url.endswith("/rest/v1/push_device_registrations"):
            return _response(
                [
                    {
                        "id": "device-1",
                        "platform": "ios",
                        "enabled": True,
                        "status": "enabled",
                        "last_seen_at": "2026-07-29T00:00:00Z",
                    }
                ]
            )
        if url.endswith("/rest/v1/user_subscriptions"):
            return _response(
                [
                    {
                        "user_id": "user-1",
                        "plan": "pro",
                        "status": "active",
                        "updated_at": "2026-07-29T00:00:00Z",
                    }
                ]
            )
        raise AssertionError(f"Unexpected URL: {url}")


class _FakeAdminUsersClientNoRole(_FakeAdminUsersClient):
    """Same fixtures as the base fake, but the profile row carries no
    role/is_admin — the realistic shape for an ordinary collector who has
    never been granted console access."""

    def request(self, method: str, url: str, **kwargs):
        if url.endswith("/rest/v1/profiles"):
            self.requests.append({"method": method, "url": url, **kwargs})
            return _response(
                [
                    {
                        "id": "user-1",
                        "display_name": "Collector One",
                        "updated_at": "2026-07-29T01:00:00Z",
                    }
                ]
            )
        return super().request(method, url, **kwargs)


def _response(payload) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("GET", "https://supabase.test"),
    )


if __name__ == "__main__":
    unittest.main()
