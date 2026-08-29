import unittest

import httpx
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app
from app.schemas.portfolio import PortfolioCreateRequest
from app.services.admin_audit_service import (
    AdminAuditService,
    SupabaseAdminAuditRepository,
    clear_in_memory_audit_events,
)
from app.services.portfolio_service import portfolio_service


class AdminAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        clear_in_memory_audit_events()
        portfolio_service._items.clear()

    def tearDown(self) -> None:
        clear_in_memory_audit_events()
        portfolio_service._items.clear()

    def test_audit_events_endpoint_requires_admin_token(self) -> None:
        response = self.client.get("/admin/audit/events")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_import_not_configured")

    def test_audit_events_endpoint_lists_in_memory_events(self) -> None:
        AdminAuditService().record(
            action="pricing_review_queue.mark_reviewed",
            status="success",
            target_type="portfolio_item",
            target_id="item-1",
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/audit/events?action=pricing_review_queue.mark_reviewed",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["events"][0]["targetId"], "item-1")

    def test_list_events_oldest_first_reverses_the_in_memory_fallback(self) -> None:
        # _IN_MEMORY_AUDIT_EVENTS is newest-first (record() inserts at
        # index 0) -- oldest_first must actually return the oldest match,
        # not just the newest N reversed in place.
        service = AdminAuditService()
        service.record(action="admin_users.role_updated", status="success", target_id="user-1")
        service.record(action="admin_users.role_updated", status="success", target_id="user-1")
        service.record(action="admin_users.role_updated", status="success", target_id="user-1")

        newest = service.list_events(target_id="user-1", limit=1)
        oldest = service.list_events(target_id="user-1", limit=1, oldest_first=True)

        self.assertNotEqual(newest["events"][0]["id"], oldest["events"][0]["id"])
        all_events = service.list_events(target_id="user-1", limit=10)["events"]
        self.assertEqual(oldest["events"][0]["id"], all_events[-1]["id"])
        self.assertEqual(newest["events"][0]["id"], all_events[0]["id"])

    def test_supabase_repository_list_events_oldest_first_sets_ascending_order(self) -> None:
        client = _FakeAuditSupabaseClient()
        repository = SupabaseAdminAuditRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        repository.list_events(oldest_first=True, limit=1)

        self.assertEqual(client.requests[-1]["params"]["order"], "created_at.asc")

    def test_audit_events_endpoint_filters_status_target_type_actor_and_dates(self) -> None:
        AdminAuditService().record(
            action="pricing_review_queue.override_price",
            status="success",
            target_type="portfolio_item",
            target_id="item-1",
            actor="admin_token",
        )
        AdminAuditService().record(
            action="scan_failure_queue.resolve",
            status="failure",
            target_type="scan_analysis",
            target_id="scan-1",
            actor="admin_token",
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/audit/events?status=success&targetType=portfolio_item&actor=admin_token",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["events"][0]["action"], "pricing_review_queue.override_price")

    def test_review_queue_action_records_audit_event(self) -> None:
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="needs-review",
                data={
                    "title": "Needs Review",
                    "category": "Card",
                    "needsReview": True,
                    "pricing": {"estimatedMarketValue": 10, "pricingConfidence": 80},
                },
            )
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.post(
                "/admin/pricing/review-queue/needs-review/reviewed",
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=pricing_review_queue.mark_reviewed",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(audit_response.status_code, 200)
        event = audit_response.json()["events"][0]
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["targetId"], "needs-review")

    def test_supabase_audit_repository_records_and_lists_events(self) -> None:
        client = _FakeAuditSupabaseClient(
            post_rows=[
                {
                    "id": "event-1",
                    "created_at": "2026-07-30T00:00:00+00:00",
                    "actor": "admin_token",
                    "action": "pricing_review_queue.retry_pricing",
                    "status": "success",
                    "target_type": "portfolio_item",
                    "target_id": "item-1",
                    "metadata": {"pricingConfidence": 92},
                }
            ],
            get_rows=[
                {
                    "id": "event-1",
                    "created_at": "2026-07-30T00:00:00+00:00",
                    "actor": "admin_token",
                    "action": "pricing_review_queue.retry_pricing",
                    "status": "success",
                    "target_type": "portfolio_item",
                    "target_id": "item-1",
                    "metadata": {"pricingConfidence": 92},
                }
            ],
        )
        repository = SupabaseAdminAuditRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )
        service = AdminAuditService(repository=repository)

        recorded = service.record(
            action="pricing_review_queue.retry_pricing",
            status="success",
            target_type="portfolio_item",
            target_id="item-1",
            metadata={"pricingConfidence": 92},
        )
        listed = service.list_events(
            action="pricing_review_queue.retry_pricing",
            status="success",
            target_type="portfolio_item",
            target_id="item-1",
            actor="admin_token",
            since="2026-07-01T00:00:00+00:00",
            until="2026-07-31T00:00:00+00:00",
        )

        self.assertEqual(recorded["id"], "event-1")
        self.assertEqual(listed["events"][0]["targetId"], "item-1")
        self.assertEqual(client.requests[0]["method"], "POST")
        self.assertTrue(client.requests[0]["url"].endswith("/rest/v1/admin_audit_events"))
        self.assertEqual(client.requests[0]["headers"]["Authorization"], "Bearer service-role")
        params = client.requests[1]["params"]
        self.assertEqual(params["action"], "eq.pricing_review_queue.retry_pricing")
        self.assertEqual(params["status"], "eq.success")
        self.assertEqual(params["target_type"], "eq.portfolio_item")
        self.assertEqual(params["target_id"], "eq.item-1")
        self.assertEqual(params["actor"], "eq.admin_token")
        self.assertIn("created_at.gte.2026-07-01", params["and"])
        self.assertIn("created_at.lte.2026-07-31", params["and"])


class _FakeAuditSupabaseClient:
    def __init__(
        self,
        *,
        post_rows: list[dict] | None = None,
        get_rows: list[dict] | None = None,
    ) -> None:
        self.post_rows = post_rows or []
        self.get_rows = get_rows or []
        self.requests: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if method == "POST":
            return _response(self.post_rows)
        if method == "GET":
            return _response(self.get_rows)
        raise AssertionError(f"Unexpected method: {method}")


def _response(payload) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("GET", "https://supabase.test"),
    )


if __name__ == "__main__":
    unittest.main()
