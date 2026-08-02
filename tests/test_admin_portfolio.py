import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.schemas.portfolio import PortfolioCreateRequest
from app.services.portfolio_service import portfolio_service


class AdminPortfolioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        portfolio_service._items.clear()

    def tearDown(self) -> None:
        portfolio_service._items.clear()

    def test_admin_portfolio_search_requires_admin_token(self) -> None:
        response = self.client.get("/admin/portfolio/items")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_import_not_configured")

    def test_admin_portfolio_search_lists_matching_items(self) -> None:
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="item-charizard",
                data={
                    "title": "Charizard Base Set",
                    "category": "Trading Card",
                    "userId": "collector-1",
                    "pricing": {
                        "estimatedMarketValue": 388,
                        "currency": "USD",
                        "pricingConfidence": 92,
                        "pricingSource": {"name": "pricecharting_catalog"},
                    },
                },
            )
        )
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="item-watch",
                data={"title": "Omega Seamaster", "category": "Watch", "userId": "collector-2"},
            )
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/portfolio/items?q=charizard",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["id"], "item-charizard")
        self.assertEqual(payload["items"][0]["price"], 388)
        self.assertEqual(payload["items"][0]["provider"], "pricecharting_catalog")

    def test_admin_portfolio_detail_returns_raw_item(self) -> None:
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="item-detail",
                data={
                    "title": "Detail Item",
                    "category": "Sneaker",
                    "userId": "collector-1",
                    "pricingAssignee": "pricing@packlox.com",
                },
            )
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/portfolio/items/item-detail",
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertEqual(item["id"], "item-detail")
        self.assertEqual(item["userId"], "collector-1")
        self.assertEqual(item["assignment"]["assignee"], "pricing@packlox.com")
        self.assertIn("raw", item)


if __name__ == "__main__":
    unittest.main()
