import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services.pricing.catalog_search_service import (
    CatalogItemNotFoundError,
    CatalogSearchService,
)


class CatalogSearchServiceTest(unittest.TestCase):
    def test_search_returns_ranked_pricecharting_results(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json=[
                    {
                        "pricecharting_id": "999",
                        "product_name": "Charizard #4 Base Set",
                        "console_name": "Pokemon Cards",
                        "category": "Pokemon Cards",
                        "upc": "",
                        "loose_price_cents": 16100,
                        "cib_price_cents": 20000,
                        "new_price_cents": None,
                        "graded_price_cents": 80000,
                        "currency": "USD",
                        "product_url": "https://www.pricecharting.com/game/pokemon/charizard",
                        "source_file": "pokemon.csv",
                        "source_downloaded_at": "2026-07-25T00:00:00Z",
                        "updated_at": "2026-07-26T00:00:00Z",
                        "normalized_identity": "charizard #4 base set pokemon cards",
                    },
                    {
                        "pricecharting_id": "111",
                        "product_name": "Dark Charizard",
                        "console_name": "Pokemon Cards",
                        "category": "Pokemon Cards",
                        "loose_price_cents": 6500,
                        "currency": "USD",
                        "source_file": "pokemon.csv",
                        "normalized_identity": "dark charizard pokemon cards",
                    },
                ],
            )

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("charizard", limit=10)

        self.assertEqual(response.count, 2)
        self.assertEqual(response.results[0].id, "999")
        self.assertEqual(response.results[0].title, "Charizard #4 Base Set")
        self.assertEqual(response.results[0].pricing.marketValue, 161)
        self.assertEqual(response.results[0].pricing.highEstimate, 800)
        self.assertEqual(response.results[0].pricing.currency, "USD")
        self.assertEqual(response.results[0].imageUrl, None)
        self.assertEqual(response.results[0].source, "PriceCharting")
        self.assertIn(
            "product_name.ilike.*charizard*",
            requests[0].url.params.get("or"),
        )

    def test_search_requests_deterministic_order_and_wider_fetch_window(self) -> None:
        # Without an explicit order, PostgreSQL doesn't guarantee row order
        # for an unordered query — seen live as a promoted row present in
        # the results on one call and absent on the next, identical call.
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        service.search("pikachu", limit=20)

        self.assertEqual(requests[0].url.params.get("order"), "product_name.asc")
        self.assertEqual(requests[0].url.params.get("limit"), "100")

    def test_search_falls_back_to_generic_price_for_scan_derived_rows(self) -> None:
        # Scan-derived rows (source_kind='scan_derived', promoted from
        # pricing_cache_entries) never populate the PriceCharting-specific
        # loose/cib/new/graded tiers — only market_value_cents/low/high_
        # estimate_cents. A result must still surface a real price and the
        # correct provider name, not silently show null/"PriceCharting".
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "pricecharting_id": "scan:abc123",
                        "product_name": "Nike Air Force 1 '07",
                        "console_name": None,
                        "category": "Sneakers",
                        "loose_price_cents": None,
                        "cib_price_cents": None,
                        "new_price_cents": None,
                        "graded_price_cents": None,
                        "currency": "USD",
                        "source_file": None,
                        "normalized_identity": "sneakers nike air force 1 07",
                        "source_provider": "kicksdb",
                        "market_value_cents": 10000,
                        "low_estimate_cents": None,
                        "high_estimate_cents": None,
                    },
                ],
            )

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("nike air force 1", limit=10)

        self.assertEqual(response.results[0].pricing.marketValue, 100)
        self.assertEqual(response.results[0].pricing.currency, "USD")
        self.assertEqual(response.results[0].source, "KicksDB")
        self.assertEqual(response.results[0].attribution, "Pricing data by KicksDB")

    def test_short_query_returns_empty_without_supabase(self) -> None:
        service = CatalogSearchService(
            supabase_url="",
            service_role_key="",
        )

        response = service.search("c", limit=10)

        self.assertEqual(response.count, 0)
        self.assertEqual(response.results, [])

    def test_detail_returns_catalog_item_with_scd2_history(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if "pricecharting_catalog_history" in str(request.url):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "valid_from": "2026-07-26T00:00:00Z",
                            "valid_to": None,
                            "is_current": True,
                            "source_file": "pokemon.csv",
                            "source_downloaded_at": "2026-07-26T00:00:00Z",
                            "loose_price_cents": 16100,
                            "cib_price_cents": 20000,
                            "new_price_cents": None,
                            "graded_price_cents": 80000,
                            "currency": "USD",
                        },
                        {
                            "valid_from": "2026-07-25T00:00:00Z",
                            "valid_to": "2026-07-26T00:00:00Z",
                            "is_current": False,
                            "source_file": "pokemon.csv",
                            "source_downloaded_at": "2026-07-25T00:00:00Z",
                            "loose_price_cents": 15000,
                            "cib_price_cents": 19000,
                            "graded_price_cents": 76000,
                            "currency": "USD",
                        },
                    ],
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "pricecharting_id": "999",
                        "product_name": "Charizard #4 Base Set",
                        "console_name": "Pokemon Cards",
                        "category": "Pokemon Cards",
                        "upc": "",
                        "loose_price_cents": 16100,
                        "cib_price_cents": 20000,
                        "new_price_cents": None,
                        "graded_price_cents": 80000,
                        "currency": "USD",
                        "product_url": "https://www.pricecharting.com/game/pokemon/charizard",
                        "source_file": "pokemon.csv",
                        "source_downloaded_at": "2026-07-26T00:00:00Z",
                        "updated_at": "2026-07-26T00:00:00Z",
                        "normalized_identity": "charizard #4 base set pokemon cards",
                    }
                ],
            )

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.detail("999", history_limit=10)

        self.assertEqual(response.result.id, "999")
        self.assertEqual(response.result.confidence, 0.96)
        self.assertEqual(response.result.pricing.marketValue, 161)
        self.assertEqual(len(response.history), 2)
        self.assertTrue(response.history[0].isCurrent)
        self.assertEqual(response.history[0].pricing.highEstimate, 800)
        self.assertEqual(response.history[1].validTo, "2026-07-26T00:00:00Z")
        self.assertIn("pricecharting_id=eq.999", str(requests[0].url))
        self.assertIn("pricecharting_catalog_history", str(requests[-1].url))
        self.assertIn("limit=10", str(requests[-1].url))

    def test_detail_missing_catalog_item_raises_not_found(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaises(CatalogItemNotFoundError) as context:
            service.detail("missing")

        self.assertIn("not found", str(context.exception).lower())


class CatalogSearchEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_catalog_search_endpoint_returns_results(self) -> None:
        with patch("app.routers.search.CatalogSearchService") as service_class:
            service_class.return_value.search.return_value = CatalogSearchService(
                supabase_url="",
                service_role_key="",
            ).search("c")

            response = self.client.get("/api/pricing/catalog/search?q=c")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["results"], [])

    def test_catalog_detail_endpoint_returns_history(self) -> None:
        with patch("app.routers.search.CatalogSearchService") as service_class:
            service_class.return_value.detail.return_value = CatalogSearchService(
                supabase_url="https://example.supabase.co",
                service_role_key="service-role",
                client=httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            200,
                            json=[
                                {
                                    "pricecharting_id": "999",
                                    "product_name": "Charizard",
                                    "console_name": "Pokemon Cards",
                                    "category": "Pokemon Cards",
                                    "loose_price_cents": 16100,
                                    "currency": "USD",
                                    "source_file": "pokemon.csv",
                                    "normalized_identity": "charizard pokemon cards",
                                }
                            ],
                        )
                    )
                ),
            ).detail("999")

            response = self.client.get("/api/pricing/catalog/999?historyLimit=5")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["result"]["id"], "999")


if __name__ == "__main__":
    unittest.main()
