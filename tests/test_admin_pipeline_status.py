import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services.admin_pipeline_status_service import (
    AdminPipelineStatusService,
    SupabasePipelineStatusRepository,
)


class AdminPipelineStatusServiceTest(unittest.TestCase):
    def test_get_summary_compacts_registry_and_kicksdb_rows(self) -> None:
        client = _FakePipelineStatusClient()
        service = AdminPipelineStatusService(
            repository=SupabasePipelineStatusRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.get_summary()

        self.assertTrue(payload["success"])
        categories = payload["priceCharting"]["categories"]
        self.assertEqual(len(categories), 2)
        wrestling = next(c for c in categories if c["category"] == "wrestling-cards")
        self.assertEqual(wrestling["totalSets"], 979)
        self.assertEqual(wrestling["completedSets"], 829)
        self.assertEqual(wrestling["percentComplete"], round(829 / 979 * 100, 1))
        totals = payload["priceCharting"]["totals"]
        self.assertEqual(totals["totalSets"], 979 + 68)
        self.assertEqual(totals["completedSets"], 829 + 68)
        self.assertEqual(payload["kicksDb"]["totalItems"], 9062)
        self.assertEqual(payload["kicksDb"]["distinctBrands"], 21)

    def test_get_summary_handles_zero_total_sets_without_dividing_by_zero(self) -> None:
        client = _FakePipelineStatusClient(registry_rows=[
            {
                "source_site": "pricecharting",
                "category": "coins",
                "total_sets": 0,
                "completed_sets": 0,
                "failed_sets": 0,
                "pending_sets": 0,
                "priority_tier": 1,
            }
        ])
        service = AdminPipelineStatusService(
            repository=SupabasePipelineStatusRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.get_summary()

        self.assertEqual(payload["priceCharting"]["categories"][0]["percentComplete"], 0.0)
        self.assertEqual(payload["priceCharting"]["totals"]["percentComplete"], 0.0)


class AdminPipelineStatusEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_pipeline_status_endpoint_returns_summary(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_catalog.AdminPipelineStatusService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.get_summary.return_value = {
                "success": True,
                "priceCharting": {"categories": [], "totals": {"totalSets": 0, "completedSets": 0, "percentComplete": 0.0}},
                "kicksDb": {"totalItems": 9062, "distinctBrands": 21, "lastRefreshedAt": None},
            }
            response = self.client.get(
                "/admin/catalog/pipelines",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kicksDb"]["totalItems"], 9062)

    def test_pipeline_status_requires_admin_token(self) -> None:
        response = self.client.get("/admin/catalog/pipelines")
        self.assertEqual(response.status_code, 503)


class _FakePipelineStatusClient:
    def __init__(self, *, registry_rows=None) -> None:
        self.registry_rows = registry_rows or [
            {
                "source_site": "sportscardspro",
                "category": "wrestling-cards",
                "total_sets": 979,
                "completed_sets": 829,
                "failed_sets": 5,
                "pending_sets": 145,
                "priority_tier": 3,
            },
            {
                "source_site": "pricecharting",
                "category": "coins",
                "total_sets": 68,
                "completed_sets": 68,
                "failed_sets": 0,
                "pending_sets": 0,
                "priority_tier": 1,
            },
        ]

    def request(self, method: str, url: str, **kwargs):
        if url.endswith("/rest/v1/rpc/admin_pricecharting_registry_summary"):
            return _response(self.registry_rows)
        if url.endswith("/rest/v1/rpc/admin_kicksdb_catalog_summary"):
            return _response([
                {
                    "total_items": 9062,
                    "distinct_brands": 21,
                    "last_refreshed_at": "2026-08-10T04:15:00Z",
                }
            ])
        raise AssertionError(f"Unexpected URL: {url}")

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)


def _response(payload) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("POST", "https://supabase.test"),
    )


if __name__ == "__main__":
    unittest.main()
