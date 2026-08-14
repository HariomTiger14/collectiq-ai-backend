import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services.admin_catalog_service import AdminCatalogService, SupabaseAdminCatalogRepository


class AdminCatalogListItemsTest(unittest.TestCase):
    # Regression: the Portfolio items screen was where the user first
    # reported "only seeing 20 items" -- the real gap turned out to be that
    # there was no way to browse the actual catalog (pricecharting_catalog +
    # kicksdb_catalog, thousands of rows from the backfill pipelines) at
    # all -- the existing "Search products" table required typing 2+ chars
    # and only ever queried pricecharting_catalog.

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_repository_browses_pricecharting_ordered_by_primary_key(self) -> None:
        # pricecharting_id.asc is deliberate -- see the comment in
        # admin_catalog_service.py: this table had five indexes dropped
        # after an unrelated unindexed sort broke production once already.
        # Only the primary key (always index-backed) is safe to order by.
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabaseAdminCatalogRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        repository.list_catalog_rows(source="pricecharting", limit=100, offset=200)

        self.assertTrue(captured["path"].endswith("/rest/v1/pricecharting_catalog"))
        self.assertEqual(captured["params"]["order"], "pricecharting_id.asc")
        self.assertEqual(captured["params"]["limit"], "100")
        self.assertEqual(captured["params"]["offset"], "200")

    def test_repository_browses_kicksdb_ordered_by_rank(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabaseAdminCatalogRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        repository.list_catalog_rows(source="kicksdb", limit=50, offset=0)

        self.assertTrue(captured["path"].endswith("/rest/v1/kicksdb_catalog"))
        self.assertEqual(captured["params"]["order"], "rank.asc.nullslast")

    def test_repository_filters_by_category(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabaseAdminCatalogRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        repository.list_catalog_rows(source="pricecharting", limit=50, offset=0, category="Pokemon")

        self.assertEqual(captured["params"]["category"], "ilike.*Pokemon*")

    def test_repository_filters_by_category_group_ors_keywords(self) -> None:
        # Regression: pricecharting_catalog's raw category column is too
        # granular for a dropdown ("Basketball Cards 2019 Panini Donruss
        # Optic", not "Sports Cards") -- category groups or-match a curated
        # keyword set against that same raw column instead.
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabaseAdminCatalogRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        repository.list_catalog_rows(source="pricecharting", limit=50, offset=0, category_group="sports-cards")

        self.assertEqual(
            captured["params"]["or"],
            "(category.ilike.*Baseball*,category.ilike.*Basketball*,category.ilike.*Football*,"
            "category.ilike.*Hockey*,category.ilike.*Soccer*)",
        )
        self.assertNotIn("category", captured["params"])

    def test_repository_category_group_ignored_for_kicksdb(self) -> None:
        # KicksDB has no equivalent taxonomy defined anywhere in this system
        # -- a category_group meant for PriceCharting shouldn't silently
        # apply to it (or=(category.ilike...) against a table where that
        # grouping was never designed to make sense).
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabaseAdminCatalogRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        repository.list_catalog_rows(source="kicksdb", limit=50, offset=0, category_group="sports-cards")

        self.assertNotIn("or", captured["params"])
        self.assertNotIn("category", captured["params"])

    def test_repository_filters_pricecharting_by_price_range_on_loose_price(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabaseAdminCatalogRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        repository.list_catalog_rows(source="pricecharting", limit=50, offset=0, min_price=10, max_price=50)

        self.assertEqual(captured["params"]["and"], "(loose_price_cents.gte.1000,loose_price_cents.lte.5000)")

    def test_repository_filters_kicksdb_by_price_range_on_avg_price(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabaseAdminCatalogRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        repository.list_catalog_rows(source="kicksdb", limit=50, offset=0, min_price=100)

        self.assertEqual(captured["params"]["avg_price_cents"], "gte.10000")
        self.assertNotIn("and", captured["params"])

    def test_repository_counts_pricecharting_rows_via_content_range(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers.get("prefer"), "count=estimated")
            return httpx.Response(200, json=[], headers={"content-range": "0-0/9284"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SupabaseAdminCatalogRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )

        total = repository.count_catalog_rows(source="pricecharting")

        self.assertEqual(total, 9284)

    def test_service_includes_total_count(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers.get("prefer") == "count=estimated":
                return httpx.Response(200, json=[], headers={"content-range": "0-0/9284"})
            return httpx.Response(
                200,
                json=[{"pricecharting_id": "1", "product_name": "Item", "currency": "USD"}],
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        self.assertEqual(payload["totalCount"], 9284)

    def test_service_compacts_pricecharting_rows(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "pricecharting_id": "12345",
                        "product_name": "Charizard Base Set",
                        "category": "Trading Card",
                        "console_name": "Base Set",
                        "upc": "820650123456",
                        "loose_price_cents": 38800,
                        "currency": "USD",
                        "updated_at": "2026-08-13T00:00:00Z",
                    }
                ],
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        self.assertEqual(payload["count"], 1)
        item = payload["items"][0]
        self.assertEqual(item["id"], "12345")
        self.assertEqual(item["title"], "Charizard Base Set")
        self.assertEqual(item["source"], "PriceCharting")
        self.assertEqual(item["pricing"]["marketValue"], 388.0)

    def test_service_compacts_kicksdb_rows(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "kicksdb_id": "air-force-1-white",
                        "title": "Nike Air Force 1 '07 White",
                        "brand": "Nike",
                        "category": "Sneaker",
                        "sku": "CW2288-111",
                        "avg_price_cents": 12000,
                        "currency": "USD",
                        "updated_at": "2026-08-13T00:00:00Z",
                    }
                ],
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=client,
            )
        )

        payload = service.list_items(source="kicksdb", limit=100, offset=0)

        item = payload["items"][0]
        self.assertEqual(item["id"], "air-force-1-white")
        self.assertEqual(item["title"], "Nike Air Force 1 '07 White")
        self.assertEqual(item["source"], "KicksDB")
        self.assertEqual(item["setName"], "Nike")
        self.assertEqual(item["pricing"]["marketValue"], 120.0)

    def test_endpoint_requires_admin_token(self) -> None:
        response = self.client.get("/admin/catalog/items")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_import_not_configured")

    def test_endpoint_rejects_unknown_source(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings:
            auth_settings.admin_import_token = "secret-token"
            response = self.client.get(
                "/admin/catalog/items?source=ebay",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 422)

    def test_endpoint_passes_params_through_to_service(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_catalog.AdminCatalogService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.list_items.return_value = {
                "success": True, "source": "kicksdb", "count": 0, "items": [],
            }
            response = self.client.get(
                "/admin/catalog/items?source=kicksdb&limit=50&offset=100",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        service.return_value.list_items.assert_called_once_with(
            source="kicksdb", limit=50, offset=100, category=None, category_group=None, min_price=None, max_price=None,
        )


if __name__ == "__main__":
    unittest.main()
