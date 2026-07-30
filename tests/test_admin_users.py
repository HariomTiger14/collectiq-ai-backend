import unittest
from unittest.mock import patch

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
        self.assertEqual(client.requests[0]["headers"]["Authorization"], "Bearer service-role")
        self.assertEqual(client.requests[0]["params"]["email"], "collector@example.com")

    def test_repository_gets_user_detail_with_recent_activity(self) -> None:
        client = _FakeAdminUsersClient()
        service = AdminUserService(
            repository=SupabaseAdminUserRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.get_user_detail("user-1")

        self.assertTrue(payload["success"])
        user = payload["user"]
        self.assertEqual(user["id"], "user-1")
        self.assertEqual(user["portfolioValue"], 125.5)
        self.assertEqual(len(user["recentPortfolioItems"]), 2)
        self.assertEqual(len(user["recentScans"]), 1)
        self.assertEqual([item["id"] for item in user["pricingReviewItems"]], ["item-2"])

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
        if url.endswith("/rest/v1/push_device_registrations"):
            return _response([{"id": "device-1"}])
        raise AssertionError(f"Unexpected URL: {url}")


def _response(payload) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("GET", "https://supabase.test"),
    )


if __name__ == "__main__":
    unittest.main()
