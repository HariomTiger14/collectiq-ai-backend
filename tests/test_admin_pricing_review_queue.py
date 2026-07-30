import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.schemas.portfolio import PortfolioCreateRequest
from app.schemas.pricing import RepricePricingResponse, RepriceResponse
from app.services.portfolio_service import portfolio_service


class AdminPricingReviewQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        portfolio_service._items.clear()

    def tearDown(self) -> None:
        portfolio_service._items.clear()

    def test_review_queue_requires_admin_token(self) -> None:
        response = self.client.get("/admin/pricing/review-queue")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_import_not_configured")

    def test_review_queue_lists_low_confidence_missing_and_stale_items(self) -> None:
        stale_date = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="low-confidence",
                data={
                    "title": "Charizard Base Set",
                    "category": "Pokemon Card",
                    "condition": "Near Mint",
                    "pricing": {
                        "estimatedMarketValue": 388,
                        "currency": "USD",
                        "pricingConfidence": 62,
                        "pricingSource": {"name": "pricecharting_catalog"},
                        "lastUpdated": datetime.now(timezone.utc).isoformat(),
                    },
                },
            )
        )
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="missing-price",
                data={
                    "title": "Rolex Submariner",
                    "category": "Watch",
                    "pricing": {
                        "estimatedMarketValue": 0,
                        "pricingConfidence": 88,
                        "pricingSource": {"name": "kicksdb"},
                    },
                },
            )
        )
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="stale-price",
                data={
                    "title": "Jordan 1 Chicago",
                    "category": "Sneaker",
                    "pricing": {
                        "estimatedMarketValue": 1200,
                        "pricingConfidence": 93,
                        "pricingSource": {"name": "ebay"},
                        "lastUpdated": stale_date,
                    },
                },
            )
        )
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="healthy",
                data={
                    "title": "Healthy Item",
                    "category": "Game",
                    "pricing": {
                        "estimatedMarketValue": 40,
                        "pricingConfidence": 91,
                        "lastUpdated": datetime.now(timezone.utc).isoformat(),
                    },
                },
            )
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/pricing/review-queue",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["totalCount"], 3)
        item_ids = {item["id"] for item in payload["items"]}
        self.assertEqual(item_ids, {"low-confidence", "missing-price", "stale-price"})
        reasons = {item["id"]: item["reasons"] for item in payload["items"]}
        self.assertIn("low_confidence", reasons["low-confidence"])
        self.assertIn("missing_price", reasons["missing-price"])
        self.assertIn("stale_price", reasons["stale-price"])

    def test_review_queue_filters_by_reason(self) -> None:
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="low-confidence",
                data={
                    "title": "Charizard",
                    "category": "Pokemon Card",
                    "pricing": {"estimatedMarketValue": 88, "pricingConfidence": 61},
                },
            )
        )
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="missing-price",
                data={
                    "title": "Unknown Watch",
                    "category": "Watch",
                    "pricing": {"estimatedMarketValue": 0, "pricingConfidence": 92},
                },
            )
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/pricing/review-queue?reason=missing_price",
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["items"]], ["missing-price"])

    def test_mark_reviewed_updates_portfolio_item(self) -> None:
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

        self.assertEqual(response.status_code, 200)
        data = portfolio_service.get_item("needs-review").data
        self.assertFalse(data["needsReview"])
        self.assertEqual(data["reviewStatus"], "reviewed")
        self.assertTrue(data["reviewedAt"])

    def test_retry_pricing_updates_item_pricing(self) -> None:
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="retry-me",
                data={
                    "title": "Retry Card",
                    "category": "Trading Card",
                    "condition": "Near Mint",
                    "pricing": {"estimatedMarketValue": 0, "pricingConfidence": 0},
                },
            )
        )

        with patch("app.routers.admin_auth.settings") as settings, patch(
            "app.services.pricing.admin_review_queue_service.RepriceService",
        ) as service:
            settings.admin_import_token = "secret-token"
            service.return_value.reprice.return_value = _reprice_response()
            response = self.client.post(
                "/admin/pricing/review-queue/retry-me/retry",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        data = portfolio_service.get_item("retry-me").data
        self.assertEqual(data["pricing"]["estimatedMarketValue"], 125.0)
        self.assertFalse(data["needsReview"])
        self.assertEqual(data["reviewStatus"], "pricing_retried")


def _reprice_response() -> RepriceResponse:
    return RepriceResponse(
        itemId="retry-me",
        correctionSource="admin_review_queue",
        identity={
            "title": "Retry Card",
            "category": "Trading Card",
        },
        pricing=RepricePricingResponse(
            status="available",
            estimatedMarketValue=125.0,
            lowEstimate=110.0,
            highEstimate=140.0,
            currency="USD",
            displayString="$125.00",
            confidenceScore=0.92,
            pricingConfidence=92,
            valuationStrategy="market_estimated",
            pricingSource={"name": "pricecharting_catalog"},
        ),
    )


if __name__ == "__main__":
    unittest.main()
