import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from scripts.import_pricecharting_catalog import CatalogSource


class AdminPriceChartingImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_import_requires_configured_admin_token(self) -> None:
        with patch("app.routers.admin_pricecharting.settings") as settings:
            settings.admin_import_token = ""

            response = self.client.post("/admin/pricecharting/import?dryRun=true")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"],
            "admin_import_not_configured",
        )

    def test_import_rejects_invalid_admin_token(self) -> None:
        with patch("app.routers.admin_pricecharting.settings") as settings:
            settings.admin_import_token = "secret-token"

            response = self.client.post(
                "/admin/pricecharting/import?dryRun=true",
                headers={"X-Admin-Token": "wrong-token"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_dry_run_returns_compact_source_counts(self) -> None:
        sources = [
            CatalogSource(
                name="video_games.csv",
                rows=[
                    {
                        "id": "12345",
                        "product-name": "Mario Kart 8 Deluxe",
                        "console-name": "Nintendo Switch",
                        "loose-price": "3150",
                    },
                ],
            ),
            CatalogSource(
                name="pokemon.csv",
                rows=[
                    {
                        "id": "999",
                        "product-name": "Charizard",
                        "console-name": "Pokemon Cards",
                        "loose-price": "120000",
                    },
                ],
            ),
        ]

        with patch("app.routers.admin_pricecharting.settings") as settings, patch(
            "app.routers.admin_pricecharting.download_env_sources",
            return_value=sources,
        ) as download_sources:
            settings.admin_import_token = "secret-token"
            settings.build_time = "2026-07-25T00:00:00Z"

            response = self.client.post(
                "/admin/pricecharting/import?dryRun=true",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["inputRows"], 2)
        self.assertEqual(payload["validRows"], 2)
        self.assertEqual(payload["importedRows"], 0)
        self.assertEqual(
            payload["sources"],
            [
                {"source": "video_games.csv", "inputRows": 1, "validRows": 1},
                {"source": "pokemon.csv", "inputRows": 1, "validRows": 1},
            ],
        )
        self.assertEqual(payload["source"], "all")
        download_sources.assert_called_once_with(timeout_seconds=30, source_filter=None)

    def test_dry_run_accepts_single_source_filter(self) -> None:
        sources = [
            CatalogSource(
                name="video_games.csv",
                rows=[
                    {
                        "id": "12345",
                        "product-name": "Mario Kart 8 Deluxe",
                        "console-name": "Nintendo Switch",
                        "loose-price": "3150",
                    },
                ],
            ),
        ]

        with patch("app.routers.admin_pricecharting.settings") as settings, patch(
            "app.routers.admin_pricecharting.download_env_sources",
            return_value=sources,
        ) as download_sources:
            settings.admin_import_token = "secret-token"
            settings.build_time = "2026-07-25T00:00:00Z"

            response = self.client.post(
                "/admin/pricecharting/import?dryRun=true&source=video-games",
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["source"], "video_games")
        self.assertEqual(payload["inputRows"], 1)
        self.assertEqual(payload["validRows"], 1)
        download_sources.assert_called_once_with(
            timeout_seconds=30,
            source_filter="video_games",
        )

    def test_dry_run_rejects_unsupported_source_filter(self) -> None:
        with patch("app.routers.admin_pricecharting.settings") as settings:
            settings.admin_import_token = "secret-token"

            response = self.client.post(
                "/admin/pricecharting/import?dryRun=true&source=watches",
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["error"]["code"],
            "unsupported_import_source",
        )


if __name__ == "__main__":
    unittest.main()
