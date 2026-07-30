import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.portfolio import PortfolioCreateRequest
from app.schemas.pricing import RepricePricingResponse, RepriceResponse
from app.services.portfolio_service import portfolio_service
from app.services.pricing.admin_review_queue_service import (
    AdminPricingReviewQueueService,
    SupabasePricingReviewQueueRepository,
)


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

    def test_supabase_review_queue_lists_persistent_portfolio_items(self) -> None:
        client = _FakeSupabaseClient(
            get_rows=[
                {
                    "id": "supabase-low",
                    "title": "Supabase Charizard",
                    "category": "Pokemon Card",
                    "condition": "Lightly Played",
                    "pricing": {
                        "estimatedMarketValue": 210,
                        "currency": "USD",
                        "pricingConfidence": 58,
                        "pricingSource": {"name": "pricecharting_catalog"},
                    },
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                {
                    "id": "supabase-healthy",
                    "title": "Healthy",
                    "category": "Game",
                    "pricing": {
                        "estimatedMarketValue": 30,
                        "pricingConfidence": 94,
                    },
                },
            ]
        )
        service = AdminPricingReviewQueueService(
            repository=SupabasePricingReviewQueueRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.list_queue()

        self.assertEqual(payload["totalCount"], 1)
        self.assertEqual(payload["items"][0]["id"], "supabase-low")
        self.assertIn("low_confidence", payload["items"][0]["reasons"])
        request = client.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertTrue(request["url"].endswith("/rest/v1/portfolio_items"))
        self.assertEqual(request["headers"]["Authorization"], "Bearer service-role")

    def test_supabase_mark_reviewed_patches_persistent_item(self) -> None:
        client = _FakeSupabaseClient(
            get_rows=[
                {
                    "id": "supabase-review",
                    "data": {
                        "title": "Review Me",
                        "category": "Card",
                        "needsReview": True,
                    },
                    "pricing": {"estimatedMarketValue": 20, "pricingConfidence": 85},
                }
            ],
            patch_rows=[
                {
                    "id": "supabase-review",
                    "data": {
                        "title": "Review Me",
                        "category": "Card",
                        "needsReview": False,
                        "reviewStatus": "reviewed",
                    },
                    "pricing": {"estimatedMarketValue": 20, "pricingConfidence": 85},
                    "review_status": "reviewed",
                }
            ],
        )
        service = AdminPricingReviewQueueService(
            repository=SupabasePricingReviewQueueRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.mark_reviewed("supabase-review")

        self.assertTrue(payload["success"])
        patch_request = client.requests[-1]
        self.assertEqual(patch_request["method"], "PATCH")
        self.assertEqual(patch_request["params"]["id"], "eq.supabase-review")
        self.assertFalse(patch_request["json"]["needs_review"])
        self.assertEqual(patch_request["json"]["review_status"], "reviewed")
        self.assertEqual(patch_request["headers"]["Prefer"], "return=representation")


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
