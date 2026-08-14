import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.portfolio import PortfolioCreateRequest
from app.services.admin_portfolio_service import AdminPortfolioService
from app.services.pricing.admin_review_queue_service import (
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
        self.assertEqual(response.json()["valuationHistory"], [])

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

    def test_repository_passes_offset_for_pagination(self) -> None:
        # Regression: the Portfolio items list had a hard limit=100 cap with
        # no way to page past it at all -- for a catalog of thousands of
        # items, admin could only ever see the first ~100 (by
        # updated_at/created_at), full stop.
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

        repository.list_items(limit=50, offset=150)

        self.assertEqual(captured["params"]["offset"], "150")

    def test_repository_count_items_reads_content_range_total(self) -> None:
        # Regression: numbered pagination (page 1, 2, 3...) needs a real
        # total, not the length of whatever batch happened to get fetched --
        # this is Supabase's Prefer: count=estimated / Content-Range mechanism.
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers.get("prefer"), "count=estimated")
            return httpx.Response(200, json=[], headers={"content-range": "0-0/1234"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabasePricingReviewQueueRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        total = repository.count_items()

        self.assertEqual(total, 1234)

    def test_service_uses_real_total_when_not_searching(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/v1/portfolio_items"):
                if request.headers.get("prefer") == "count=estimated":
                    return httpx.Response(200, json=[], headers={"content-range": "0-0/500"})
                return httpx.Response(200, json=[{"id": "item-1", "user_id": "user-1"}])
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

        self.assertEqual(payload["totalCount"], 500)

    def test_service_falls_back_to_batch_length_while_searching(self) -> None:
        # A real DB total wouldn't reflect the search filter (client-side,
        # not a DB filter -- see PR #81), so a search in progress keeps the
        # old fetched-batch-length approximation instead of a misleading
        # whole-table count.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/v1/portfolio_items"):
                self.assertNotEqual(request.headers.get("prefer"), "count=estimated")
                return httpx.Response(
                    200,
                    json=[{"id": "item-1", "user_id": "user-1", "title": "Charizard"}],
                )
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        service = AdminPortfolioService(
            repository=SupabasePricingReviewQueueRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.list_items(query="charizard", limit=50)

        self.assertEqual(payload["totalCount"], 1)

    def test_service_passes_offset_through_to_repository(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/v1/portfolio_items"):
                if request.headers.get("prefer") == "count=estimated":
                    return httpx.Response(200, json=[], headers={"content-range": "0-0/250"})
                assert request.url.params.get("offset") == "100"
                return httpx.Response(200, json=[{"id": f"item-{i}", "user_id": "user-1"} for i in range(50)])
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        service = AdminPortfolioService(
            repository=SupabasePricingReviewQueueRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.list_items(limit=50, offset=100)

        self.assertEqual(payload["count"], 50)

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

    def test_get_item_includes_valuation_history_for_that_item_only(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/v1/portfolio_items"):
                return httpx.Response(
                    200,
                    json=[{"id": "item-1", "user_id": "user-42", "title": "Charizard"}],
                )
            if request.url.path.endswith("/rest/v1/portfolio_valuation_snapshots"):
                captured["params"] = dict(request.url.params)
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "snap-1",
                            "portfolio_item_id": "item-1",
                            "value_aud": 217.0,
                            "display_string": "AUD $217.00",
                            "valuation_status": "market_estimated",
                            "valuation_strategy": "market_estimated",
                            "priced_at": "2026-08-13T00:00:00Z",
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        service = AdminPortfolioService(
            repository=SupabasePricingReviewQueueRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.get_item("item-1")

        self.assertEqual(captured["params"]["portfolio_item_id"], "eq.item-1")
        self.assertEqual(len(payload["valuationHistory"]), 1)
        self.assertEqual(payload["valuationHistory"][0]["displayString"], "AUD $217.00")

    def test_get_item_resolves_owner_display_name(self) -> None:
        # Regression: list_items() resolved a real name/email for the Owner
        # column (batch display-name lookup, falling back to a single Auth
        # email lookup), but get_item() -- the single-item detail page --
        # never got the same treatment, so the exact same item showed a
        # resolved owner on the list and a raw UUID on its own detail page.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/v1/portfolio_items"):
                return httpx.Response(200, json=[{"id": "item-1", "user_id": "user-42", "title": "Air Force 1"}])
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

        payload = service.get_item("item-1")

        self.assertEqual(payload["item"]["ownerEmail"], "Jordan T.")

    def test_get_item_falls_back_to_email_when_no_display_name(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/v1/portfolio_items"):
                return httpx.Response(200, json=[{"id": "item-1", "user_id": "user-42", "title": "Air Force 1"}])
            if request.url.path.endswith("/rest/v1/collector_profiles"):
                return httpx.Response(200, json=[])  # no display name set
            if request.url.path.endswith("/auth/v1/admin/users/user-42"):
                return httpx.Response(200, json={"id": "user-42", "email": "collector@example.com"})
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        service = AdminPortfolioService(
            repository=SupabasePricingReviewQueueRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.get_item("item-1")

        self.assertEqual(payload["item"]["ownerEmail"], "collector@example.com")

    def test_valuation_history_pages_through_everything_not_just_first_page(self) -> None:
        # Regression: this used to cap at a fixed limit=30, truncating the
        # chart/list to the 30 most recent snapshots even though the table
        # itself retains every snapshot ever priced -- 1M/6M/MAX ranges are
        # meaningless if the data behind them is already cut off. Simulates
        # more than one page (page size is 500) to prove it actually pages
        # through instead of stopping after the first request.
        requests_seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params.get("offset", "0"))
            requests_seen.append(offset)
            if offset == 0:
                rows = [
                    {"id": f"snap-{i}", "portfolio_item_id": "item-1", "value_aud": float(i), "priced_at": "2026-08-01T00:00:00Z"}
                    for i in range(500)
                ]
            elif offset == 500:
                rows = [{"id": "snap-500", "portfolio_item_id": "item-1", "value_aud": 500.0, "priced_at": "2026-08-01T00:00:00Z"}]
            else:
                rows = []
            return httpx.Response(200, json=rows)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabasePricingReviewQueueRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        rows = repository.list_valuation_history_for_item("item-1")

        self.assertEqual(requests_seen, [0, 500])
        self.assertEqual(len(rows), 501)

    def test_endpoint_passes_user_id_query_param_through(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_portfolio.AdminPortfolioService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.list_items.return_value = {
                "success": True, "query": "", "count": 0, "totalCount": 0, "items": [],
            }
            response = self.client.get(
                "/admin/portfolio/items?userId=user-42",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        service.return_value.list_items.assert_called_once_with(query=None, limit=50, user_id="user-42", offset=0)


if __name__ == "__main__":
    unittest.main()
