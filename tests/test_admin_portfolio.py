import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.portfolio import PortfolioCreateRequest
from app.services.admin_portfolio_service import AdminPortfolioService
from app.services.pricing.admin_review_queue_service import (
    ReviewQueueRepositoryError,
    SupabasePricingReviewQueueRepository,
)
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

    def test_admin_portfolio_update_writes_editable_fields(self) -> None:
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="item-update",
                data={
                    "title": "Editable Item",
                    "category": "Unknown",
                    "condition": "Unknown",
                    "userId": "collector-1",
                },
            )
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.patch(
                "/admin/portfolio/items/item-update",
                headers={"Authorization": "Bearer secret-token"},
                json={
                    "category": "Sneakers",
                    "condition": "Near Mint",
                    "adminNotes": "Verified from admin portal.",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["updated"])
        item = payload["item"]
        self.assertEqual(item["category"], "Sneakers")
        self.assertEqual(item["condition"], "Near Mint")
        self.assertEqual(item["adminNotes"], "Verified from admin portal.")

    def test_admin_portfolio_update_ignores_pricing_and_workflow_fields(self) -> None:
        # Regression test: price/currency/confidence/pricingProvider/
        # valuationStatus/reviewStatus used to be editable through this
        # general-purpose endpoint with no note requirement — a second,
        # looser door to the same fields the Pricing Review Queue's
        # override flow already controls with a mandatory note + typed
        # confirmation. A client sending them now must have no effect.
        portfolio_service.add_item(
            PortfolioCreateRequest(
                id="item-locked-fields",
                data={
                    "title": "Locked Fields Item",
                    "category": "Trading Card",
                    "userId": "collector-1",
                    "pricing": {
                        "estimatedMarketValue": 100,
                        "currency": "USD",
                        "pricingConfidence": 90,
                        "pricingSource": {"name": "pricecharting_catalog"},
                    },
                },
            )
        )

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"
            response = self.client.patch(
                "/admin/portfolio/items/item-locked-fields",
                headers={"Authorization": "Bearer secret-token"},
                json={
                    "category": "Sneakers",
                    "valuationStatus": "reviewed",
                    "reviewStatus": "reviewed",
                    "price": 9999,
                    "currency": "AUD",
                    "confidence": 12,
                    "pricingProvider": "admin_override",
                },
            )

        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertEqual(item["category"], "Sneakers")  # the one field that IS editable did change
        self.assertEqual(item["price"], 100)  # untouched — provider price, not the attempted 9999
        self.assertEqual(item["currency"], "USD")
        self.assertEqual(item["confidence"], 90)
        self.assertEqual(item["provider"], "pricecharting_catalog")


    def test_repository_filters_items_by_user_id(self) -> None:
        # There was previously no way to see all of a specific user's
        # portfolio items in the admin console — the list endpoint had no
        # owner filter at all, only a title-text search. This is the
        # backend half of adding one.
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabasePricingReviewQueueRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        repository.list_items(limit=50, user_id="user-42")

        self.assertEqual(captured["params"]["user_id"], "eq.user-42")

    def test_repository_row_parsing_includes_user_id(self) -> None:
        # Regression test: _portfolio_item_from_row copied over title,
        # category, condition, etc. from the raw Supabase row but forgot
        # user_id — so every item's "owner" showed the literal string
        # "Unknown" instead of the real (filterable) user_id, even though
        # the column itself was always there and correctly used for
        # filtering (test above).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "item-1",
                        "user_id": "user-42",
                        "title": "Charizard",
                        "category": "Trading Card",
                    }
                ],
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabasePricingReviewQueueRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        items = repository.list_items(limit=50)

        self.assertEqual(items[0].data["userId"], "user-42")

    def test_repository_batches_owner_display_names(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json=[
                    {"user_id": "user-1", "display_name": "Collector One"},
                    {"user_id": "user-2", "display_name": ""},  # no display name set — dropped
                ],
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabasePricingReviewQueueRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        names = repository.batch_owner_display_names(["user-1", "user-2", "user-1"])  # dupes collapsed

        self.assertEqual(captured["params"]["user_id"], "in.(user-1,user-2)")
        self.assertEqual(names, {"user-1": "Collector One"})

    def test_list_items_fills_owner_field_with_display_name(self) -> None:
        # End-to-end: a real Supabase-backed list_items() call should show
        # a human-readable owner instead of "Unknown" once both fixes above
        # are wired together.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/v1/portfolio_items"):
                return httpx.Response(
                    200,
                    json=[{"id": "item-1", "user_id": "user-42", "title": "Charizard", "category": "Trading Card"}],
                )
            if request.url.path.endswith("/rest/v1/collector_profiles"):
                return httpx.Response(200, json=[{"user_id": "user-42", "display_name": "Jordan T."}])
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        service = AdminPortfolioService(
            repository=SupabasePricingReviewQueueRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.list_items(limit=50)

        item = payload["items"][0]
        self.assertEqual(item["userId"], "user-42")
        self.assertEqual(item["ownerEmail"], "Jordan T.")

    def test_endpoint_passes_user_id_query_param_through(self) -> None:
        user_id = "372c586d-4658-4144-8969-e450322e622d"
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_portfolio.AdminPortfolioService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.list_items.return_value = {
                "success": True, "query": "", "count": 0, "totalCount": 0, "items": [],
            }
            response = self.client.get(
                f"/admin/portfolio/items?userId={user_id}",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        service.return_value.list_items.assert_called_once_with(query=None, limit=50, user_id=user_id)

    def test_endpoint_rejects_non_uuid_user_id_with_a_clean_422(self) -> None:
        # Regression test: Supabase/PostgREST rejects a non-UUID `eq.`
        # filter on the user_id column with its own 500, which used to
        # surface here as an unhandled crash instead of a clean error.
        # Verified live against production before this fix landed.
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_portfolio.AdminPortfolioService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/portfolio/items?userId=totally-fake-user-id-xyz",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_user_id")
        service.return_value.list_items.assert_not_called()

    def test_endpoint_returns_503_when_repository_fails(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_portfolio.AdminPortfolioService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.list_items.side_effect = ReviewQueueRepositoryError("Supabase request failed.")
            response = self.client.get(
                "/admin/portfolio/items",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_portfolio_items_unavailable")


if __name__ == "__main__":
    unittest.main()
