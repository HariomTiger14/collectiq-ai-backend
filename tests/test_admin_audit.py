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
        listed = service.list_events(action="pricing_review_queue.retry_pricing")

        self.assertEqual(recorded["id"], "event-1")
        self.assertEqual(listed["events"][0]["targetId"], "item-1")
        self.assertEqual(client.requests[0]["method"], "POST")
        self.assertTrue(client.requests[0]["url"].endswith("/rest/v1/admin_audit_events"))
        self.assertEqual(client.requests[0]["headers"]["Authorization"], "Bearer service-role")
        self.assertEqual(client.requests[1]["params"]["action"], "eq.pricing_review_queue.retry_pricing")


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
