import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services.admin_subscription_metrics_service import (
    AdminSubscriptionMetricsService,
    SupabaseSubscriptionMetricsRepository,
)


class AdminSubscriptionMetricsServiceTest(unittest.TestCase):
    def test_get_summary_groups_plan_status_source_and_counts_overrides(self) -> None:
        client = _FakeSubscriptionMetricsClient()
        audit_service = _FakeAuditService(count=3)
        service = AdminSubscriptionMetricsService(
            repository=SupabaseSubscriptionMetricsRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            ),
            audit_service=audit_service,
        )

        payload = service.get_summary()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["totalUsers"], 120)
        free_bucket = next(b for b in payload["byPlan"] if b["label"] == "free")
        pro_bucket = next(b for b in payload["byPlan"] if b["label"] == "pro")
        self.assertEqual(free_bucket["count"], 100)
        self.assertEqual(free_bucket["percent"], round(100 / 120 * 100, 1))
        self.assertEqual(pro_bucket["count"], 20)
        active_bucket = next(b for b in payload["byStatus"] if b["label"] == "active")
        self.assertEqual(active_bucket["count"], 120)
        admin_override_bucket = next(b for b in payload["bySource"] if b["label"] == "admin_override")
        self.assertEqual(admin_override_bucket["count"], 5)
        self.assertEqual(payload["recentAdminOverrides"], {"days": 30, "count": 3})

    def test_get_summary_computes_mrr_and_pro_conversion_from_pro_count_only(self) -> None:
        # 20 pro users at the real app price (AUD 9.99/mo); premium is
        # deliberately excluded (no confirmed live price -- it's the "2
        # legacy" bucket the console itself already flags as not currently
        # sold).
        client = _FakeSubscriptionMetricsClient()
        service = AdminSubscriptionMetricsService(
            repository=SupabaseSubscriptionMetricsRepository(
                supabase_url="https://supabase.test", service_role_key="service-role", client=client,
            ),
            audit_service=_FakeAuditService(count=3),
        )

        payload = service.get_summary()

        self.assertEqual(payload["mrrAud"], round(20 * 9.99, 2))
        self.assertEqual(payload["proConversionPercent"], round(20 / 120 * 100, 1))
        self.assertEqual(payload["activeAdminOverrides"], 5)

    def test_upgrades_this_month_counts_only_free_to_paid_transitions(self) -> None:
        client = _FakeSubscriptionMetricsClient()
        audit_service = _FakeAuditService(
            count=3,
            events=[
                {"metadata": {"fromPlan": "free", "toPlan": "pro"}},
                {"metadata": {"fromPlan": "free", "toPlan": "premium"}},
                # Not an upgrade -- a downgrade and a lateral admin change.
                {"metadata": {"fromPlan": "pro", "toPlan": "free"}},
                {"metadata": {"fromPlan": "pro", "toPlan": "pro"}},
            ],
        )
        service = AdminSubscriptionMetricsService(
            repository=SupabaseSubscriptionMetricsRepository(
                supabase_url="https://supabase.test", service_role_key="service-role", client=client,
            ),
            audit_service=audit_service,
        )

        payload = service.get_summary()

        self.assertEqual(payload["upgradesThisMonth"], 2)

    def test_recent_entitlement_changes_includes_user_email_and_plan_transition(self) -> None:
        client = _FakeSubscriptionMetricsClient()
        audit_service = _FakeAuditService(
            count=0,
            events=[
                {
                    "targetId": "user-1",
                    "metadata": {"fromPlan": "free", "toPlan": "pro", "source": "admin_override"},
                    "createdAt": "2026-08-16T14:20:00Z",
                },
            ],
        )
        user_repository = Mock()
        user_repository._get_auth_user.return_value = {"id": "user-1", "email": "jordan@example.com"}
        service = AdminSubscriptionMetricsService(
            repository=SupabaseSubscriptionMetricsRepository(
                supabase_url="https://supabase.test", service_role_key="service-role", client=client,
            ),
            audit_service=audit_service,
            user_repository=user_repository,
        )

        payload = service.get_summary()

        self.assertEqual(len(payload["recentEntitlementChanges"]), 1)
        change = payload["recentEntitlementChanges"][0]
        self.assertEqual(change["userLabel"], "jordan@example.com")
        self.assertEqual(change["fromPlan"], "free")
        self.assertEqual(change["toPlan"], "pro")
        self.assertEqual(change["source"], "admin_override")

    def test_get_summary_handles_zero_users_without_dividing_by_zero(self) -> None:
        client = _FakeSubscriptionMetricsClient(rows=[])
        service = AdminSubscriptionMetricsService(
            repository=SupabaseSubscriptionMetricsRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            ),
            audit_service=_FakeAuditService(count=0),
        )

        payload = service.get_summary()

        self.assertEqual(payload["totalUsers"], 0)
        self.assertEqual(payload["byPlan"], [])


class AdminSubscriptionSummaryEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_endpoint_returns_summary_and_is_not_shadowed_by_user_id_route(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminSubscriptionMetricsService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.get_summary.return_value = {
                "success": True,
                "totalUsers": 120,
                "byPlan": [{"label": "free", "count": 100, "percent": 83.3}],
                "byStatus": [],
                "bySource": [],
                "recentAdminOverrides": {"days": 30, "count": 3},
            }
            response = self.client.get(
                "/admin/users/subscription-summary",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["totalUsers"], 120)

    def test_endpoint_requires_admin_token(self) -> None:
        response = self.client.get("/admin/users/subscription-summary")
        self.assertEqual(response.status_code, 503)


class _FakeSubscriptionMetricsClient:
    def __init__(self, *, rows=None) -> None:
        self.rows = rows if rows is not None else [
            {"plan": "free", "status": "active", "source": "none", "total": 95},
            {"plan": "free", "status": "active", "source": "mock", "total": 5},
            {"plan": "pro", "status": "active", "source": "google_play", "total": 10},
            {"plan": "pro", "status": "active", "source": "app_store", "total": 5},
            {"plan": "pro", "status": "active", "source": "admin_override", "total": 5},
        ]

    def post(self, url: str, **kwargs):
        if url.endswith("/rest/v1/rpc/admin_subscription_summary"):
            return httpx.Response(
                status_code=200,
                json=self.rows,
                request=httpx.Request("POST", "https://supabase.test"),
            )
        raise AssertionError(f"Unexpected URL: {url}")


class _FakeAuditService:
    def __init__(self, *, count: int, events: list | None = None) -> None:
        self._count = count
        self._events = events or []

    def list_events(self, **kwargs):
        return {"success": True, "count": self._count, "events": self._events}


if __name__ == "__main__":
    unittest.main()
