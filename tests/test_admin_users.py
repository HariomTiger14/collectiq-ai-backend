import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services.admin_audit_service import clear_in_memory_audit_events
from app.services.admin_user_service import (
    AdminUserService,
    SupabaseAdminUserRepository,
    _compact_price_alert,
    _compact_valuation_snapshot,
)


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

    def test_list_users_batches_enrichment_across_the_page(self) -> None:
        # Regression test: the list endpoint used to make ~6 sequential
        # per-user requests (profile, collector_profile, 3 counts each
        # fetching up to 1000 full rows, subscription) — a 50-user page was
        # ~300 round-trips to Supabase. It must now make exactly one batched
        # request per table across the WHOLE page, using an `in.()` filter
        # over every user_id, regardless of how many users are on the page.
        client = _FakeAdminUsersClient()
        service = AdminUserService(
            repository=SupabaseAdminUserRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.list_users(query="collector@example.com", limit=10)

        for table in ("portfolio_items", "scan_analysis_events", "push_device_registrations", "user_subscriptions"):
            table_requests = [r for r in client.requests if r["url"].endswith(f"/rest/v1/{table}")]
            self.assertEqual(len(table_requests), 1, f"expected exactly 1 batched request for {table}")
            self.assertIn("in.(user-1)", table_requests[0]["params"]["user_id"])

        user = payload["users"][0]
        self.assertEqual(user["portfolioCount"], 2)
        self.assertEqual(user["scanCount"], 1)
        self.assertEqual(user["pushDeviceCount"], 1)
        self.assertEqual(user["plan"], "pro")

    def test_get_user_detail_still_uses_per_user_count_requests(self) -> None:
        # The single-user detail path has no N+1 concern (there's only one
        # user), so it keeps the per-user count=exact request rather than
        # going through the batch path meant for list pages.
        client = _FakeAdminUsersClient()
        subscription_service = Mock()
        subscription_service.get_entitlement.return_value = {"plan": "pro", "status": "active", "source": "mock", "currentPeriodEnd": None}
        subscription_service.get_scan_usage.return_value = {"used": 0, "limit": 30, "periodStart": "2026-07-01"}
        service = AdminUserService(
            repository=SupabaseAdminUserRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            ),
            subscription_service=subscription_service,
        )

        service.get_user_detail("user-1")

        count_requests = [
            r for r in client.requests
            if r["url"].endswith("/rest/v1/portfolio_items") and r["params"].get("limit") == "1"
        ]
        self.assertEqual(len(count_requests), 1)
        self.assertEqual(count_requests[0]["headers"]["Prefer"], "count=exact")

    def test_list_users_reports_has_more_when_page_is_full(self) -> None:
        client = _FakeAdminUsersClient()
        service = AdminUserService(
            repository=SupabaseAdminUserRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        # The fixture's /auth/v1/admin/users response always returns exactly
        # one user, so limit=1 makes this page "full" (hasMore) and limit=10
        # makes it "not full" (last page).
        full_page = service.list_users(query=None, limit=1, page=1)
        partial_page = service.list_users(query=None, limit=10, page=1)

        self.assertTrue(full_page["hasMore"])
        self.assertFalse(partial_page["hasMore"])
        self.assertEqual(full_page["page"], 1)

    def test_list_users_passes_page_param_to_gotrue(self) -> None:
        client = _FakeAdminUsersClient()
        service = AdminUserService(
            repository=SupabaseAdminUserRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        service.list_users(query=None, limit=25, page=3)

        users_request = next(r for r in client.requests if r["url"].endswith("/auth/v1/admin/users"))
        self.assertEqual(users_request["params"]["page"], "3")
        self.assertEqual(users_request["params"]["per_page"], "25")

    def test_users_list_endpoint_accepts_page_query_param(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.list_users.return_value = {
                "success": True, "query": "", "page": 2, "count": 0, "hasMore": False, "users": [],
            }
            response = self.client.get(
                "/admin/users?page=2",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["page"], 2)
        service.return_value.list_users.assert_called_once_with(query=None, limit=50, page=2)

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
        # displayName in the summary must come from collector_profiles (the
        # table the app actually writes), not the admin role table.
        self.assertEqual(user["displayName"], "Collector One")
        self.assertEqual(user["collectorProfile"]["displayName"], "Collector One")
        self.assertEqual(user["collectorProfile"]["countryCode"], "AU")
        self.assertEqual(user["collectorProfile"]["preferredCurrency"], "AUD")
        # The avatar bucket is private — admin needs a signed URL, not the
        # raw storage path, to actually display the user's photo.
        self.assertEqual(
            user["collectorProfile"]["avatarSignedUrl"],
            "https://supabase.test/storage/v1/object/sign/collectiq-portfolio-images/users/user-1/profile/avatar.jpg?token=fake-signed-token",
        )
        self.assertEqual(len(user["wishlistEntries"]), 1)
        self.assertEqual(user["wishlistEntries"][0]["status"], "wanted")
        self.assertEqual(len(user["valuationHistory"]), 1)
        self.assertEqual(user["valuationHistory"][0]["valueAud"], 125.5)
        # Regression: valuation snapshot rows only ever stored
        # portfolio_item_id, not a title, so the admin page couldn't tell
        # which item a history entry belonged to.
        self.assertEqual(user["valuationHistory"][0]["itemTitle"], "Healthy Card")
        self.assertEqual(len(user["pushDeliveries"]), 1)
        self.assertEqual(user["pushDeliveries"][0]["status"], "sent")

    def test_compact_price_alert_includes_percentage(self) -> None:
        # Regression test: percentage-rule alerts (e.g. "notify me on a 10%
        # rise") showed a blank % field on the admin user-detail page even
        # though the value was saved — _compact_price_alert dropped the
        # `percentage` column while still returning `targetAmount`, so
        # amount-rule alerts looked fine and percentage-rule alerts didn't.
        alert = _compact_price_alert(
            {
                "id": "alert-2",
                "item_title": "Air Force 1",
                "rule_type": "percentageIncrease",
                "target_amount": None,
                "percentage": 10,
                "enabled": True,
                "status": "active",
            }
        )

        self.assertEqual(alert["percentage"], 10)

    def test_compact_valuation_snapshot_falls_back_when_item_title_unknown(self) -> None:
        # A snapshot can outlive the item it priced (item deleted, or its id
        # just wasn't in the batch lookup) -- should read as an explicit
        # "unknown", not a blank title.
        snapshot = _compact_valuation_snapshot(
            {"id": "snap-2", "portfolio_item_id": "item-missing", "value_aud": 10},
            item_titles={},
        )

        self.assertEqual(snapshot["itemTitle"], "Deleted or unknown item")

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

    def test_collector_profile_update_endpoint_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.update_collector_profile.return_value = {
                "success": True,
                "userId": "user-1",
                "collectorProfile": {"displayName": "New Name", "countryCode": "US"},
            }
            response = self.client.patch(
                "/admin/users/user-1/collector-profile",
                json={"displayName": "New Name", "countryCode": "US"},
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_users.collector_profile_updated",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["collectorProfile"]["displayName"], "New Name")
        service.return_value.update_collector_profile.assert_called_once_with(
            user_id="user-1", display_name="New Name", country_code="US", preferred_currency=None
        )
        event = audit_response.json()["events"][0]
        self.assertEqual(event["metadata"]["displayName"], "New Name")

    def test_wishlist_entry_update_endpoint_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.update_wishlist_entry.return_value = {
                "success": True,
                "userId": "user-1",
                "itemId": "item-1",
                "entry": {"status": "owned"},
            }
            response = self.client.patch(
                "/admin/users/user-1/wishlist/item-1",
                json={"status": "owned"},
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_users.wishlist_entry_updated",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entry"]["status"], "owned")
        service.return_value.update_wishlist_entry.assert_called_once_with(
            user_id="user-1", item_id="item-1", status="owned"
        )
        event = audit_response.json()["events"][0]
        self.assertEqual(event["metadata"]["status"], "owned")

    def test_wishlist_entry_update_rejects_invalid_status(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings:
            auth_settings.admin_import_token = "secret-token"
            response = self.client.patch(
                "/admin/users/user-1/wishlist/item-1",
                json={"status": "sold"},
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 422)

    def test_wishlist_entry_delete_endpoint_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.delete_wishlist_entry.return_value = {
                "success": True,
                "userId": "user-1",
                "itemId": "item-1",
                "deleted": True,
            }
            response = self.client.delete(
                "/admin/users/user-1/wishlist/item-1",
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_users.wishlist_entry_deleted",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])
        service.return_value.delete_wishlist_entry.assert_called_once_with(
            user_id="user-1", item_id="item-1"
        )
        self.assertEqual(len(audit_response.json()["events"]), 1)

    def test_price_alert_update_endpoint_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.update_price_alert.return_value = {
                "success": True,
                "userId": "user-1",
                "alertId": "alert-1",
                "alert": {"enabled": False},
            }
            response = self.client.patch(
                "/admin/users/user-1/price-alerts/alert-1",
                json={"enabled": False},
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_users.price_alert_updated",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["alert"]["enabled"])
        service.return_value.update_price_alert.assert_called_once_with(
            user_id="user-1",
            alert_id="alert-1",
            fields={
                "enabled": False,
                "status": None,
                "target_amount": None,
                "percentage": None,
                "message": None,
            },
        )
        event = audit_response.json()["events"][0]
        self.assertEqual(event["metadata"]["enabled"], False)

    def test_price_alert_delete_endpoint_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.delete_price_alert.return_value = {
                "success": True,
                "userId": "user-1",
                "alertId": "alert-1",
                "deleted": True,
            }
            response = self.client.delete(
                "/admin/users/user-1/price-alerts/alert-1",
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_users.price_alert_deleted",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])
        service.return_value.delete_price_alert.assert_called_once_with(
            user_id="user-1", alert_id="alert-1"
        )
        self.assertEqual(len(audit_response.json()["events"]), 1)

    def test_repository_update_collector_profile_patches_supabase(self) -> None:
        client = _FakeAdminUsersClient()
        repo = SupabaseAdminUserRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        result = repo.update_collector_profile(
            user_id="user-1", fields={"display_name": "New Name"}
        )

        self.assertEqual(result["display_name"], "Collector One")  # fake returns its canned row
        patch_request = next(r for r in client.requests if r["method"] == "PATCH" and r["url"].endswith("/rest/v1/collector_profiles"))
        self.assertEqual(patch_request["params"]["user_id"], "eq.user-1")
        self.assertEqual(patch_request["json"], {"display_name": "New Name"})

    def test_repository_delete_wishlist_entry_soft_deletes(self) -> None:
        client = _FakeAdminUsersClient()
        repo = SupabaseAdminUserRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        repo.delete_wishlist_entry(user_id="user-1", item_id="item-1")

        patch_request = next(r for r in client.requests if r["method"] == "PATCH" and r["url"].endswith("/rest/v1/collector_wishlist_entries"))
        self.assertEqual(patch_request["json"], {"deleted": True})
        self.assertEqual(patch_request["params"]["portfolio_item_id"], "eq.item-1")

    def test_service_delete_price_alert_uses_soft_disable_fields(self) -> None:
        client = _FakeAdminUsersClient()
        service = AdminUserService(
            repository=SupabaseAdminUserRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        service.delete_price_alert(user_id="user-1", alert_id="alert-1")

        patch_request = next(r for r in client.requests if r["method"] == "PATCH" and r["url"].endswith("/rest/v1/price_alerts"))
        self.assertEqual(patch_request["json"], {"status": "paused", "enabled": False})


class _FakeAdminUsersClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if "/storage/v1/object/sign/" in url:
            return _response({"signedURL": "/object/sign/collectiq-portfolio-images/users/user-1/profile/avatar.jpg?token=fake-signed-token"})
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
            # The admin/console-role table — deliberately carries no
            # display_name in this fixture, matching reality (the mobile app
            # never writes to this table; collector_profiles is the real
            # source of a user's display name).
            return _response(
                [
                    {
                        "id": "user-1",
                        "role": "admin",
                        "is_admin": True,
                        "updated_at": "2026-07-29T01:00:00Z",
                    }
                ]
            )
        if url.endswith("/rest/v1/collector_profiles"):
            return _response(
                [
                    {
                        "user_id": "user-1",
                        "display_name": "Collector One",
                        "avatar_path": "users/user-1/profile/avatar.jpg",
                        "country_code": "AU",
                        "preferred_currency": "AUD",
                        "updated_at": "2026-07-29T02:00:00Z",
                    }
                ]
            )
        if url.endswith("/rest/v1/collector_wishlist_entries"):
            return _response(
                [
                    {
                        "user_id": "user-1",
                        "portfolio_item_id": "item-1",
                        "title": "Healthy Card",
                        "category": "Card",
                        "status": "wanted",
                        "deleted": False,
                        "updated_at": "2026-07-29T00:00:00Z",
                    }
                ]
            )
        if url.endswith("/rest/v1/portfolio_valuation_snapshots"):
            return _response(
                [
                    {
                        "id": "snap-1",
                        "portfolio_item_id": "item-1",
                        "value_aud": 125.5,
                        "display_string": "$125.50 AUD",
                        "valuation_status": "market_estimated",
                        "valuation_strategy": "sold_completed",
                        "priced_at": "2026-07-28T00:00:00Z",
                    }
                ]
            )
        if url.endswith("/rest/v1/push_notification_deliveries"):
            return _response(
                [
                    {
                        "id": "delivery-1",
                        "price_alert_id": "alert-1",
                        "portfolio_item_id": "item-1",
                        "title": "Price alert triggered",
                        "body": "Charizard rose above $500.",
                        "status": "sent",
                        "sent_at": "2026-07-29T00:05:00Z",
                        "created_at": "2026-07-29T00:05:00Z",
                    }
                ]
            )
        if url.endswith("/rest/v1/portfolio_items"):
            return _response(
                [
                    {
                        "id": "item-1",
                        "user_id": "user-1",
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
                        "user_id": "user-1",
                        "title": "Needs Review",
                        "category": "Card",
                        "needs_review": True,
                        "pricing": {
                            "estimatedMarketValue": 0,
                            "currency": "USD",
                            "pricingConfidence": 20,
                        },
                    },
                ],
                headers={"content-range": "0-1/2"},
            )
        if url.endswith("/rest/v1/scan_analysis_events"):
            return _response(
                [{"id": "scan-1", "user_id": "user-1", "status": "failed", "provider": "openai"}],
                headers={"content-range": "0-0/1"},
            )
        if url.endswith("/rest/v1/price_alerts"):
            return _response(
                [
                    {
                        "id": "alert-1",
                        "item_title": "Charizard",
                        "portfolio_item_id": "item-1",
                        "rule_type": "priceRisesAboveAmount",
                        "target_amount": 500,
                        "percentage": None,
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
                        "user_id": "user-1",
                        "platform": "ios",
                        "enabled": True,
                        "status": "enabled",
                        "last_seen_at": "2026-07-29T00:00:00Z",
                    }
                ],
                headers={"content-range": "0-0/1"},
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


def _response(payload, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=payload,
        headers=headers,
        request=httpx.Request("GET", "https://supabase.test"),
    )


if __name__ == "__main__":
    unittest.main()
