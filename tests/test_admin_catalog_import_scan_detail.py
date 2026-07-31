import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.admin_audit_service import clear_in_memory_audit_events
from app.services.admin_import_job_service import AdminImportJobService
from app.services.admin_scan_failure_service import clear_in_memory_scans, seed_in_memory_scan


class AdminCatalogImportScanDetailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        clear_in_memory_audit_events()
        clear_in_memory_scans()

    def tearDown(self) -> None:
        clear_in_memory_audit_events()
        clear_in_memory_scans()

    def test_catalog_update_endpoint_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_catalog.AdminCatalogService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.update_item.return_value = {
                "success": True,
                "itemId": "999",
                "item": {"pricecharting_id": "999", "product_name": "Charizard"},
            }

            response = self.client.patch(
                "/admin/catalog/999",
                headers={"Authorization": "Bearer secret-token"},
                json={"title": "Charizard", "category": "Pokemon", "note": "Admin correction"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_catalog.item_updated",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["itemId"], "999")
        self.assertEqual(audit_response.json()["events"][0]["targetId"], "999")

    def test_import_jobs_endpoint_lists_in_memory_jobs(self) -> None:
        service = AdminImportJobService()
        job = service.create_job(source="pokemon", dry_run=True)
        service.complete_job(job["id"], {"inputRows": 10, "validRows": 8})

        with patch("app.routers.admin_auth.settings") as auth_settings:
            auth_settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/pricecharting/import-jobs",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["jobs"][0]["source"], "pokemon")

    def test_scan_failure_detail_returns_history_and_records_audit(self) -> None:
        seed_in_memory_scan(
            {
                "id": "scan-1",
                "title": "Blurry Charizard",
                "status": "failed",
                "confidence": 42,
                "createdAt": "2026-07-31T00:00:00Z",
                "retryRequestedAt": "2026-07-31T01:00:00Z",
                "imageUrl": "https://example.test/scan.jpg",
            }
        )

        with patch("app.routers.admin_auth.settings") as auth_settings:
            auth_settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/scans/failures/scan-1",
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=scan_failure_queue.detail_viewed",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scan"]["id"], "scan-1")
        self.assertEqual(response.json()["scan"]["history"][0]["event"], "retry_requested")
        self.assertEqual(audit_response.json()["events"][0]["targetId"], "scan-1")


if __name__ == "__main__":
    unittest.main()
