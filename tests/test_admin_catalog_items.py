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

    def test_repository_counts_pricecharting_rows_via_content_range(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers.get("prefer"), "count=exact")
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
            if request.headers.get("prefer") == "count=exact":
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
        service.return_value.list_items.assert_called_once_with(source="kicksdb", limit=50, offset=100)


if __name__ == "__main__":
    unittest.main()
