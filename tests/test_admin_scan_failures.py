import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services.admin_audit_service import clear_in_memory_audit_events
from app.services.admin_scan_failure_service import (
    AdminScanFailureService,
    SupabaseScanFailureRepository,
    clear_in_memory_scans,
    seed_in_memory_scan,
)


class AdminScanFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        clear_in_memory_scans()
        clear_in_memory_audit_events()

    def tearDown(self) -> None:
        clear_in_memory_scans()
        clear_in_memory_audit_events()

    def test_scan_failures_require_admin_token(self) -> None:
        response = self.client.get("/admin/scans/failures")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_import_not_configured")

    def test_scan_failures_list_and_filter_in_memory_scans(self) -> None:
        seed_in_memory_scan(
            {
                "id": "low-scan",
                "title": "Blurry Charizard",
                "category": "Pokemon Card",
                "confidence": 42,
                "detectionQuality": "Blurry image",
                "pricing": {"estimatedMarketValue": 0},
                "aiProvider": "gemini",
            }
        )
        seed_in_memory_scan(
            {
                "id": "healthy-scan",
                "title": "Healthy",
                "category": "Game",
                "confidence": 94,
                "pricing": {"estimatedMarketValue": 40},
            }
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/scans/failures?reason=image_quality",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["totalCount"], 1)
        self.assertEqual(payload["items"][0]["id"], "low-scan")
        self.assertIn("image_quality", payload["items"][0]["reasons"])

    def test_mark_scan_failure_reviewed_records_audit_event(self) -> None:
        seed_in_memory_scan(
            {
                "id": "scan-review",
                "title": "Review Scan",
                "category": "Card",
                "status": "failed",
                "errorCode": "AI_PROVIDER_INVALID_RESPONSE",
            }
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.post(
                "/admin/scans/failures/scan-review/reviewed",
                headers={"Authorization": "Bearer secret-token"},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=scan_failure_queue.mark_reviewed",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        event = audit_response.json()["events"][0]
        self.assertEqual(event["targetId"], "scan-review")
        self.assertEqual(event["status"], "success")

    def test_resolve_scan_failure_records_category_note_and_audit(self) -> None:
        seed_in_memory_scan(
            {
                "id": "scan-resolve",
                "title": "Resolve Scan",
                "category": "Card",
                "status": "failed",
                "errorCode": "AI_PROVIDER_INVALID_RESPONSE",
                "rawError": {"message": "provider failed", "apiKey": "secret-key"},
            }
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.post(
                "/admin/scans/failures/scan-resolve/resolved",
                headers={"Authorization": "Bearer secret-token"},
                json={"category": "provider_error", "note": "Provider outage was confirmed."},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=scan_failure_queue.resolve",
                headers={"Authorization": "Bearer secret-token"},
            )
            queue_response = self.client.get(
                "/admin/scans/failures",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reviewStatus"], "resolved")
        self.assertEqual(response.json()["category"], "provider_error")
        self.assertEqual(audit_response.json()["events"][0]["targetId"], "scan-resolve")
        self.assertEqual(queue_response.json()["totalCount"], 0)

    def test_scan_failure_payload_redacts_raw_error_details(self) -> None:
        seed_in_memory_scan(
            {
                "id": "scan-raw",
                "title": "Raw Error",
                "status": "failed",
                "rawError": {"message": "timeout", "token": "bearer secret"},
            }
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/scans/failures",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["rawError"]["token"], "[redacted]")

    def test_retry_scan_failure_requires_image_reference(self) -> None:
        seed_in_memory_scan(
            {
                "id": "scan-no-image",
                "title": "No Image",
                "category": "Card",
                "status": "failed",
            }
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.post(
                "/admin/scans/failures/scan-no-image/retry",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "scan_failure_queue_unavailable")

    def test_supabase_scan_failure_repository_lists_and_patches(self) -> None:
        client = _FakeSupabaseClient(
            get_rows=[
                {
                    "id": "scan-1",
                    "title": "Provider Failed",
                    "category": "Coin",
                    "analysis_status": "failed",
                    "error_code": "timeout",
                    "image_url": "https://uploads.test/scan.jpg",
                    "confidence": 0,
                }
            ],
            patch_rows=[
                {
                    "id": "scan-1",
                    "title": "Provider Failed",
                    "category": "Coin",
                    "analysis_status": "failed",
                    "review_status": "retry_requested",
                    "image_url": "https://uploads.test/scan.jpg",
                    "confidence": 0,
                }
            ],
        )
        service = AdminScanFailureService(
            repository=SupabaseScanFailureRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        listed = service.list_failures()
        retried = service.retry_analysis("scan-1")
        resolved = service.resolve_failure("scan-1", category="provider_error", note="Done.")

        self.assertEqual(listed["items"][0]["id"], "scan-1")
        self.assertIn("provider_error", listed["items"][0]["reasons"])
        self.assertEqual(retried["status"], "retry_requested")
        self.assertEqual(resolved["reviewStatus"], "resolved")
        self.assertEqual(client.requests[0]["method"], "GET")
        self.assertTrue(client.requests[0]["url"].endswith("/rest/v1/scan_analysis_events"))
        self.assertEqual(client.requests[-1]["method"], "PATCH")
        self.assertEqual(client.requests[-1]["headers"]["Authorization"], "Bearer service-role")
        self.assertEqual(client.requests[-1]["json"]["review_status"], "resolved")
        self.assertEqual(client.requests[-1]["json"]["triage_category"], "provider_error")
        self.assertEqual(client.requests[-1]["json"]["resolution_note"], "Done.")


class _FakeSupabaseClient:
    def __init__(
        self,
        *,
        get_rows: list[dict] | None = None,
        patch_rows: list[dict] | None = None,
    ) -> None:
        self.get_rows = get_rows or []
        self.patch_rows = patch_rows or []
        self.requests: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if method == "GET":
            return _response(self.get_rows)
        if method == "PATCH":
            return _response(self.patch_rows)
        raise AssertionError(f"Unexpected method: {method}")


def _response(payload) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("GET", "https://supabase.test"),
    )


if __name__ == "__main__":
    unittest.main()
