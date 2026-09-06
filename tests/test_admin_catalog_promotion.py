import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from tests.admin_auth_helpers import console_admin, static_job_token
from app.services.pricing.promote_scan_derived_catalog import PromotionResult


class AdminCatalogPromotionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_requires_configured_job_token(self) -> None:
        """This is a scheduled job, so it reports the JOB token as missing.

        It used to authenticate with the human import token, which is what
        kept ADMIN_IMPORT_TOKEN load-bearing for automation.
        """
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_job_token = ""
            settings.admin_import_token = ""
            settings.supabase_url = ""
            settings.supabase_anon_key = ""
            settings.supabase_service_role_key = ""

            response = self.client.post(
                "/admin/catalog/promote-scan-derived?dryRun=true"
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"], "admin_job_not_configured"
        )

    def test_rejects_invalid_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_job_token = "secret-token"
            settings.admin_import_token = "a-different-import-token"

            response = self.client.post(
                "/admin/catalog/promote-scan-derived?dryRun=true",
                headers={"X-Admin-Token": "wrong-token"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_dry_run_returns_promotion_summary(self) -> None:
        with static_job_token() as settings, patch(
            "app.routers.admin_catalog_promotion.promote_scan_derived_rows",
            return_value=PromotionResult(
                candidateCount=5,
                promotedCount=0,
                skippedAlreadyPromoted=1,
                skippedMissingTitle=1,
            ),
        ) as promote:

            response = self.client.post(
                "/admin/catalog/promote-scan-derived?dryRun=true&minHitCount=2",
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["minHitCount"], 2)
        self.assertEqual(payload["candidateCount"], 5)
        self.assertEqual(payload["promotedRows"], 0)
        self.assertEqual(payload["skippedAlreadyPromoted"], 1)
        self.assertEqual(payload["skippedMissingTitle"], 1)
        promote.assert_called_once_with(
            min_hit_count=2, dry_run=True, limit=500, timeout_seconds=30
        )


if __name__ == "__main__":
    unittest.main()
