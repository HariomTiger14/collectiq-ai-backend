import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.pricing.portfolio_catalog_matching_service import MatchResult


class AdminPortfolioMatchingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_requires_configured_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_job_token = ""

            response = self.client.post("/admin/portfolio/match-catalog?dryRun=true")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_job_not_configured")

    def test_rejects_invalid_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_job_token = "secret-token"

            response = self.client.post(
                "/admin/portfolio/match-catalog?dryRun=true",
                headers={"X-Admin-Token": "wrong-token"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_dry_run_returns_match_summary(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings, patch(
            "app.routers.admin_portfolio_matching.match_unlinked_portfolio_items",
            return_value=MatchResult(
                candidateCount=10,
                matchedCount=4,
                unmatchedCount=5,
                skippedMissingTitle=1,
            ),
        ) as match_fn:
            settings.admin_job_token = "secret-token"

            response = self.client.post(
                "/admin/portfolio/match-catalog?dryRun=true&limit=50",
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["candidateCount"], 10)
        self.assertEqual(payload["matchedCount"], 4)
        self.assertEqual(payload["unmatchedCount"], 5)
        self.assertEqual(payload["skippedMissingTitle"], 1)
        match_fn.assert_called_once_with(limit=50, dry_run=True, timeout_seconds=30)


if __name__ == "__main__":
    unittest.main()
