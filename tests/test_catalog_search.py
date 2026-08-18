import json
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services.pricing.catalog_search_service import (
    CatalogItemNotFoundError,
    CatalogSearchService,
    _funko_lookup_title,
    _lego_set_number,
    _magic_card_name,
    _magic_card_number,
    _normalize_magic_text,
    _pokemon_card_number,
    _lorcana_set_name_from_console,
    _onepiece_set_code,
    _pokemon_variant_token,
    _yugioh_set_code,
)


class CatalogSearchServiceTest(unittest.TestCase):
    def test_search_returns_ranked_pricecharting_results(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if "search_kicksdb_catalog" in str(request.url):
                return httpx.Response(200, json=[])
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
        self.assertEqual(requests[0].method, "POST")
        self.assertIn(
            "/rest/v1/rpc/search_pricecharting_catalog", str(requests[0].url)
        )
        body = json.loads(requests[0].content)
        self.assertEqual(body["search_query"], "charizard")
        self.assertEqual(body["result_limit"], 10)

    def test_search_calls_the_db_side_ranking_rpc(self) -> None:
        # Two single-column ORDER BY attempts on the plain REST table query
        # were tried and reverted: product_name.asc forced an unindexed
        # sort and broke production; pricecharting_id.asc would have been
        # fast but systematically hidden every scan-derived row from
        # popular queries (their ids sort after all-digit PriceCharting
        # ids). This test pins search() to the RPC-based fix instead —
        # search_pricecharting_catalog() ranks the true full matching set
        # in SQL (backed by pg_trgm indexes, verified via EXPLAIN ANALYZE:
        # 'pikachu v' went from 15.7s to 83ms), so there's no arbitrary
        # fetch-window subset to guess at, and no single id/name column
        # driving the sort. See docs/GLOBAL_CATALOG_ARCHITECTURE.md.
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

        self.assertEqual(requests[0].method, "POST")
        self.assertIn(
            "/rest/v1/rpc/search_pricecharting_catalog", str(requests[0].url)
        )

    def test_search_falls_back_to_generic_price_for_scan_derived_rows(self) -> None:
        # Scan-derived rows (source_kind='scan_derived', promoted from
        # pricing_cache_entries) never populate the PriceCharting-specific
        # loose/cib/new/graded tiers — only market_value_cents/low/high_
        # estimate_cents. A result must still surface a real price and the
        # correct provider name, not silently show null/"PriceCharting".
        def handler(request: httpx.Request) -> httpx.Response:
            if "search_kicksdb_catalog" in str(request.url):
                return httpx.Response(200, json=[])
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

    def test_search_merges_real_kicksdb_catalog_results_with_images(self) -> None:
        # kicksdb_catalog is a separate table from pricecharting_catalog
        # (confirmed 100% image_url coverage live, 11,415/11,415 rows) —
        # this pins search() to actually querying it and surfacing a real
        # product image, not just the scan-derived-row relabeling covered
        # by the previous test.
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if "search_kicksdb_catalog" in str(request.url):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "kicksdb_id": "kdb-1",
                            "title": "Air Jordan 1 Retro High OG",
                            "brand": "Jordan",
                            "model": "Air Jordan 1",
                            "category": "Sneakers",
                            "product_type": "shoe",
                            "image_url": "https://images.kicks.dev/air-jordan-1.png",
                            "currency": "USD",
                            "min_price_cents": 22000,
                            "max_price_cents": 41000,
                            "avg_price_cents": 31000,
                            "product_url": "https://kicks.dev/air-jordan-1",
                            "sku": "555088-134",
                            "updated_at": "2026-08-10T00:00:00Z",
                        },
                    ],
                )
            if "search_pricecharting_catalog" in str(request.url):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("air jordan 1", limit=10)

        self.assertEqual(response.count, 1)
        self.assertEqual(response.results[0].id, "kdb-1")
        self.assertEqual(response.results[0].source, "KicksDB")
        self.assertEqual(
            response.results[0].imageUrl, "https://images.kicks.dev/air-jordan-1.png"
        )
        self.assertEqual(response.results[0].pricing.marketValue, 310)
        self.assertEqual(response.results[0].pricing.lowEstimate, 220)
        self.assertEqual(response.results[0].pricing.highEstimate, 410)
        self.assertTrue(
            any("search_kicksdb_catalog" in str(r.url) for r in requests)
        )

    def test_detail_falls_back_to_kicksdb_catalog_with_image(self) -> None:
        # When a catalog id isn't a pricecharting_catalog row at all, detail()
        # must try kicksdb_catalog before raising not-found — this is the
        # path that lets a sneaker catalog detail page show a real photo.
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if "kicksdb_catalog_history" in str(request.url):
                return httpx.Response(200, json=[])
            if "kicksdb_catalog" in str(request.url):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "kicksdb_id": "kdb-1",
                            "title": "Air Jordan 1 Retro High OG",
                            "brand": "Jordan",
                            "category": "Sneakers",
                            "image_url": "https://images.kicks.dev/air-jordan-1.png",
                            "currency": "USD",
                            "min_price_cents": 22000,
                            "max_price_cents": 41000,
                            "avg_price_cents": 31000,
                            "product_url": "https://kicks.dev/air-jordan-1",
                            "sku": "555088-134",
                            "updated_at": "2026-08-10T00:00:00Z",
                        },
                    ],
                )
            # pricecharting_catalog lookup: no matching row
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.detail("kdb-1")

        self.assertEqual(response.result.id, "kdb-1")
        self.assertEqual(response.result.source, "KicksDB")
        self.assertEqual(
            response.result.imageUrl, "https://images.kicks.dev/air-jordan-1.png"
        )
        self.assertEqual(response.history, [])

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
        history_requests = [
            request
            for request in requests
            if "pricecharting_catalog_history" in str(request.url)
        ]
        self.assertEqual(len(history_requests), 1)
        self.assertIn("limit=10", str(history_requests[0].url))

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

    def test_detail_enriches_funko_result_with_real_image(self) -> None:
        # PriceCharting has real pricing for Funko Pop rows but no image
        # field at all (confirmed live, zero image data in raw_payload for
        # any category). funko_pop_catalog is a static reference table
        # (imported from the open-source funko-pop-data dataset) used only
        # to attach a real photo when a confident exact-title match exists.
        # This enrichment only runs in detail() (a bounded per-item
        # identification use, reached when a user taps into a specific
        # catalog item to confirm it before saving to their portfolio) --
        # not in the open, free-to-everyone search() browse surface.
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            url = str(request.url)
            if "funko_pop_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "image_url": "https://images.hobbydb.com/1950-batmobile.png",
                            "series": ["Funko Vinyl Art Toys"],
                        }
                    ],
                )
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "7531531",
                            "product_name": "1950 Batmobile #277",
                            "console_name": "Funko POP Rides",
                            "category": "Batman: 80th Anniversary, Amazon Exclusive",
                            "loose_price_cents": 4500,
                            "currency": "USD",
                            "normalized_identity": "1950 batmobile funko pop rides",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.detail("7531531")

        self.assertEqual(response.result.id, "7531531")
        self.assertEqual(
            response.result.imageUrl,
            "https://images.hobbydb.com/1950-batmobile.png",
        )
        self.assertTrue(
            any("funko_pop_catalog" in str(r.url) for r in requests)
        )

    def test_detail_leaves_funko_result_unenriched_when_no_match(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])  # no match found
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "99999",
                            "product_name": "Some Obscure Figure #1",
                            "console_name": "Funko POP Rides",
                            "category": "Misc",
                            "loose_price_cents": 1000,
                            "currency": "USD",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.detail("99999")

        self.assertIsNone(response.result.imageUrl)

    def test_detail_skips_funko_lookup_for_non_funko_results(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            url = str(request.url)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "999",
                            "product_name": "Charizard #4 Base Set",
                            "console_name": "Pokemon Cards",
                            "category": "Pokemon Cards",
                            "loose_price_cents": 16100,
                            "currency": "USD",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        service.detail("999")

        self.assertFalse(
            any("funko_pop_catalog" in str(r.url) for r in requests)
        )

    def test_search_never_attaches_publisher_card_images_inline(self) -> None:
        # Regression test locking in the risk-reduction decision made in
        # search(): it is the open, free-to-everyone catalog browse
        # surface, so it must never set imageUrl (safe-to-render-inline)
        # from any of the _enrich_with_*_image methods -- even when a
        # matching reference-table row exists. Publisher-sourced card/
        # product images (Funko/Pokemon/LEGO/Magic/Yu-Gi-Oh/Lorcana/One
        # Piece) are only ever rendered inline via detail(), a bounded
        # per-item identification use reached by tapping into one specific
        # catalog item. search() DOES still resolve the same match and
        # expose it as externalImageUrl -- link-only, opened externally by
        # the client, never rendered inline. See _resolve_external_image_url.
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "funko_pop_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "image_url": "https://images.hobbydb.com/1950-batmobile.png",
                            "series": ["Funko Vinyl Art Toys"],
                        }
                    ],
                )
            if "search_pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "7531531",
                            "product_name": "1950 Batmobile #277",
                            "console_name": "Funko POP Rides",
                            "category": "Batman: 80th Anniversary, Amazon Exclusive",
                            "loose_price_cents": 4500,
                            "currency": "USD",
                            "normalized_identity": "1950 batmobile funko pop rides",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("1950 batmobile", limit=10)

        self.assertEqual(response.results[0].id, "7531531")
        self.assertIsNone(response.results[0].imageUrl)
        self.assertEqual(
            response.results[0].externalImageUrl,
            "https://images.hobbydb.com/1950-batmobile.png",
        )
        self.assertTrue(
            any("funko_pop_catalog" in str(r.url) for r in requests)
        )

    def test_search_gates_external_image_url_by_admin_flag(self) -> None:
        # Same flag-gating contract as CatalogImageFlagsGatingTest's
        # detail() coverage, but for the externalImageUrl path in
        # search(): a disabled category must suppress externalImageUrl
        # exactly like it suppresses imageUrl in detail().
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "catalog_image_source_flags" in url:
                return httpx.Response(
                    200, json=[{"category": "funko", "enabled": False}]
                )
            if "funko_pop_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "image_url": "https://images.hobbydb.com/1950-batmobile.png",
                            "series": ["Funko Vinyl Art Toys"],
                        }
                    ],
                )
            if "search_pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "7531531",
                            "product_name": "1950 Batmobile #277",
                            "console_name": "Funko POP Rides",
                            "category": "Batman: 80th Anniversary, Amazon Exclusive",
                            "loose_price_cents": 4500,
                            "currency": "USD",
                            "normalized_identity": "1950 batmobile funko pop rides",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("1950 batmobile", limit=10)

        self.assertIsNone(response.results[0].imageUrl)
        self.assertIsNone(response.results[0].externalImageUrl)

    def test_search_does_not_set_external_image_url_when_kicksdb_already_has_image(
        self,
    ) -> None:
        # KicksDB (sneaker) rows already carry a real imageUrl assigned
        # directly by _kicksdb_row_to_result, not via the publisher-image
        # enrichment chain. _resolve_external_image_url must leave those
        # alone -- imageUrl already covers the "show the user a picture"
        # need, so externalImageUrl should stay unset.
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_pricecharting_catalog" in url:
                return httpx.Response(200, json=[])
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "search_kicksdb_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "kicksdb_id": "sneaker-1",
                            "title": "Air Jordan 1 Retro High",
                            "brand": "Nike",
                            "image_url": "https://kicksdb.example.com/aj1.jpg",
                            "lowest_ask_cents": 20000,
                            "currency": "USD",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("air jordan 1", limit=10)

        self.assertEqual(len(response.results), 1)
        self.assertEqual(
            response.results[0].imageUrl, "https://kicksdb.example.com/aj1.jpg"
        )
        self.assertIsNone(response.results[0].externalImageUrl)


class PokemonImageEnrichmentTest(unittest.TestCase):
    # tcgplayer_pokemon_catalog (imported from TCGCSV — see
    # scripts/import_tcgplayer_pokemon_catalog.py) is our own Supabase
    # table, so every scenario below routes through the same `client`
    # mock as the pricecharting_catalog/funko lookups, keyed off the
    # request path/method rather than a separate external client. This
    # enrichment only runs in detail() now, so the row fetch goes through
    # _fetch_catalog_row's GET /rest/v1/pricecharting_catalog?pricecharting_id=eq.<id>
    # shape, not the search_pricecharting_catalog RPC.

    def _handler(self, *, search_row: dict, tcgplayer_rows: dict, sibling_rows: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                group_name = request.url.params.get("group_name", "").removeprefix("eq.")
                card_number = request.url.params.get("card_number", "").removeprefix("eq.")
                return httpx.Response(
                    200, json=tcgplayer_rows.get((group_name, card_number), [])
                )
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                if "pricecharting_id" in request.url.params:
                    return httpx.Response(200, json=[search_row])
                console_name = request.url.params.get("console_name", "").removeprefix("eq.")
                return httpx.Response(200, json=sibling_rows.get(console_name, []))
            return httpx.Response(200, json=[])

        return handler

    def test_detail_uses_plain_image_when_no_sibling_variant_rows(self) -> None:
        search_row = {
            "pricecharting_id": "630417",
            "product_name": "Charizard #4",
            "console_name": "Pokemon Base Set",
            "category": "Pokemon Card",
            "loose_price_cents": 16100,
            "currency": "USD",
        }
        tcgplayer_rows = {
            ("Base Set", "4"): [
                {
                    "product_name": "Charizard",
                    "image_url": "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
                    "variant_tag": None,
                },
            ],
        }
        # Only this one row shares Base Set + #4 -- no siblings.
        sibling_rows = {"Pokemon Base Set": [{"pricecharting_id": "630417"}]}

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(
                        search_row=search_row,
                        tcgplayer_rows=tcgplayer_rows,
                        sibling_rows=sibling_rows,
                    )
                )
            ),
        )

        response = service.detail("630417")

        self.assertEqual(
            response.result.imageUrl,
            "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
        )

    def test_detail_suppresses_generic_image_when_sibling_variant_rows_exist(self) -> None:
        # Real production shape: Base Set Charizard #4 has 5 PriceCharting
        # rows (plain, [1999-2000], [1st Edition], [Shadowless], [Black
        # Dot Error]). This one has no bracket tag of its own and no
        # exact TCGCSV match, but its siblings exist -- so even the
        # "plain" generic image must be suppressed, not just the tagged
        # rows, since TCGplayer's one photo can't be trusted to represent
        # whichever specific print this untagged row actually is.
        search_row = {
            "pricecharting_id": "630417",
            "product_name": "Charizard #4",
            "console_name": "Pokemon Base Set",
            "category": "Pokemon Card",
            "loose_price_cents": 16100,
            "currency": "USD",
        }
        tcgplayer_rows = {
            ("Base Set", "4"): [
                {
                    "product_name": "Charizard",
                    "image_url": "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
                    "variant_tag": None,
                },
            ],
        }
        sibling_rows = {
            "Pokemon Base Set": [
                {"pricecharting_id": "630417"},
                {"pricecharting_id": "7096109"},  # [1999-2000]
            ]
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(
                        search_row=search_row,
                        tcgplayer_rows=tcgplayer_rows,
                        sibling_rows=sibling_rows,
                    )
                )
            ),
        )

        response = service.detail("630417")

        self.assertIsNone(response.result.imageUrl)

    def test_detail_uses_exact_shadowless_image(self) -> None:
        search_row = {
            "pricecharting_id": "715695",
            "product_name": "Charizard [Shadowless] #4",
            "console_name": "Pokemon Base Set",
            "category": "Pokemon Card",
            "loose_price_cents": 300000,
            "currency": "USD",
        }
        tcgplayer_rows = {
            ("Base Set (Shadowless)", "4"): [
                {
                    "product_name": "Charizard",
                    "image_url": "https://tcgplayer-cdn.tcgplayer.com/product/106999_200w.jpg",
                    "variant_tag": "shadowless",
                },
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(
                        search_row=search_row, tcgplayer_rows=tcgplayer_rows, sibling_rows={}
                    )
                )
            ),
        )

        response = service.detail("715695")

        self.assertEqual(
            response.result.imageUrl,
            "https://tcgplayer-cdn.tcgplayer.com/product/106999_200w.jpg",
        )

    def test_detail_uses_exact_error_variant_image(self) -> None:
        search_row = {
            "pricecharting_id": "7307451",
            "product_name": "Charizard [Black Dot Error] #4",
            "console_name": "Pokemon Base Set",
            "category": "Pokemon Card",
            "loose_price_cents": 495000,
            "currency": "USD",
        }
        tcgplayer_rows = {
            ("Base Set", "4"): [
                {
                    "product_name": "Charizard",
                    "image_url": "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
                    "variant_tag": None,
                },
                {
                    "product_name": "Charizard (Black Dot Error)",
                    "image_url": "https://tcgplayer-cdn.tcgplayer.com/product/657516_200w.jpg",
                    "variant_tag": "error",
                },
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(
                        search_row=search_row, tcgplayer_rows=tcgplayer_rows, sibling_rows={}
                    )
                )
            ),
        )

        response = service.detail("7307451")

        self.assertEqual(
            response.result.imageUrl,
            "https://tcgplayer-cdn.tcgplayer.com/product/657516_200w.jpg",
        )

    def test_detail_skips_unmapped_pokemon_set(self) -> None:
        tcgplayer_requests: list[httpx.Request] = []

        def pc_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "tcgplayer_pokemon_catalog" in url:
                tcgplayer_requests.append(request)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "3438352",
                            "product_name": "Charizard [1st Edition] #103",
                            "console_name": "Pokemon Japanese Expedition Expansion Pack",
                            "category": "Pokemon Card",
                            "loose_price_cents": 5000,
                            "currency": "USD",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(pc_handler)),
        )

        response = service.detail("3438352")

        self.assertIsNone(response.result.imageUrl)
        self.assertEqual(len(tcgplayer_requests), 0)


class CatalogImageFlagsGatingTest(unittest.TestCase):
    # Covers detail()'s admin-controlled per-category kill switch
    # (_fetch_enabled_image_categories in catalog_search_service.py),
    # reusing the same Pokemon/Base-Set fixture shape as
    # PokemonImageEnrichmentTest above since it's the simplest enrichment
    # path with a single, unambiguous match.

    def _handler(self, *, search_row: dict, tcgplayer_rows: dict, flags_response: httpx.Response):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "catalog_image_source_flags" in url:
                return flags_response
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                group_name = request.url.params.get("group_name", "").removeprefix("eq.")
                card_number = request.url.params.get("card_number", "").removeprefix("eq.")
                return httpx.Response(
                    200, json=tcgplayer_rows.get((group_name, card_number), [])
                )
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                if "pricecharting_id" in request.url.params:
                    return httpx.Response(200, json=[search_row])
                # No sibling variant rows for this fixture -- keeps the
                # plain match unambiguous, same as the "no siblings" case
                # in PokemonImageEnrichmentTest.
                return httpx.Response(200, json=[{"pricecharting_id": search_row["pricecharting_id"]}])
            return httpx.Response(200, json=[])

        return handler

    def _search_and_tcgplayer_rows(self) -> tuple[dict, dict]:
        search_row = {
            "pricecharting_id": "630417",
            "product_name": "Charizard #4",
            "console_name": "Pokemon Base Set",
            "category": "Pokemon Card",
            "loose_price_cents": 16100,
            "currency": "USD",
        }
        tcgplayer_rows = {
            ("Base Set", "4"): [
                {
                    "product_name": "Charizard",
                    "image_url": "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
                    "variant_tag": None,
                },
            ],
        }
        return search_row, tcgplayer_rows

    def test_detail_skips_enrichment_when_category_disabled_via_flags(self) -> None:
        search_row, tcgplayer_rows = self._search_and_tcgplayer_rows()
        flags_response = httpx.Response(
            200, json=[{"category": "pokemon", "enabled": False}]
        )

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(
                        search_row=search_row,
                        tcgplayer_rows=tcgplayer_rows,
                        flags_response=flags_response,
                    )
                )
            ),
        )

        response = service.detail("630417")

        self.assertIsNone(response.result.imageUrl)

    def test_detail_fails_open_and_still_enriches_when_flags_fetch_errors(self) -> None:
        search_row, tcgplayer_rows = self._search_and_tcgplayer_rows()
        flags_response = httpx.Response(500, json={"message": "boom"})

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(
                        search_row=search_row,
                        tcgplayer_rows=tcgplayer_rows,
                        flags_response=flags_response,
                    )
                )
            ),
        )

        response = service.detail("630417")

        self.assertEqual(
            response.result.imageUrl,
            "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
        )

    def test_detail_fails_open_and_still_enriches_when_flags_table_empty(self) -> None:
        search_row, tcgplayer_rows = self._search_and_tcgplayer_rows()
        flags_response = httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(
                        search_row=search_row,
                        tcgplayer_rows=tcgplayer_rows,
                        flags_response=flags_response,
                    )
                )
            ),
        )

        response = service.detail("630417")

        self.assertEqual(
            response.result.imageUrl,
            "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
        )


class PokemonVariantTokenTest(unittest.TestCase):
    def test_extracts_bracket_tag(self) -> None:
        self.assertEqual(_pokemon_variant_token("Charizard [Shadowless] #4"), "shadowless")

    def test_returns_none_without_brackets(self) -> None:
        self.assertIsNone(_pokemon_variant_token("Charizard #4"))


class LegoImageEnrichmentTest(unittest.TestCase):
    # rebrickable_lego_catalog (imported from Rebrickable's free bulk
    # export -- see scripts/import_rebrickable_lego_catalog.py) is our own
    # Supabase table, so this routes through the same `client` mock as
    # every other lookup, keyed off the request path. This enrichment
    # only runs in detail() now, so the row fetch goes through
    # _fetch_catalog_row's GET /rest/v1/pricecharting_catalog?pricecharting_id=eq.<id>
    # shape, not the search_pricecharting_catalog RPC.

    def _handler(self, *, search_row: dict, lego_rows: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                base_number = request.url.params.get("base_number", "").removeprefix("eq.")
                return httpx.Response(200, json=lego_rows.get(base_number, []))
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(200, json=[search_row])
            return httpx.Response(200, json=[])

        return handler

    def test_detail_uses_image_when_title_words_overlap(self) -> None:
        search_row = {
            "pricecharting_id": "5873213",
            "product_name": "Altair #7322",
            "console_name": "LEGO Space",
            "category": "LEGO Space",
            "loose_price_cents": 4500,
            "currency": "USD",
        }
        lego_rows = {
            "7322": [
                {
                    "name": "Altair",
                    "image_url": "https://cdn.rebrickable.com/media/sets/7322-1.jpg",
                },
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(self._handler(search_row=search_row, lego_rows=lego_rows))
            ),
        )

        response = service.detail("5873213")

        self.assertEqual(
            response.result.imageUrl,
            "https://cdn.rebrickable.com/media/sets/7322-1.jpg",
        )

    def test_detail_rejects_number_match_without_word_overlap(self) -> None:
        # Real production case: PriceCharting's "Roof Bricks #445" collides
        # on set number with Rebrickable's unrelated "Police Units" --
        # matching on number alone would show a completely wrong photo.
        search_row = {
            "pricecharting_id": "111",
            "product_name": "Roof Bricks #445",
            "console_name": "LEGO Classic",
            "category": "LEGO Classic",
            "loose_price_cents": 2000,
            "currency": "USD",
        }
        lego_rows = {
            "445": [
                {
                    "name": "Police Units",
                    "image_url": "https://cdn.rebrickable.com/media/sets/445-1.jpg",
                },
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(self._handler(search_row=search_row, lego_rows=lego_rows))
            ),
        )

        response = service.detail("111")

        self.assertIsNone(response.result.imageUrl)

    def test_detail_skips_non_lego_set(self) -> None:
        lego_requests: list[httpx.Request] = []

        def pc_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "rebrickable_lego_catalog" in url:
                lego_requests.append(request)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "999",
                            "product_name": "Some Game #445",
                            "console_name": "Nintendo 64",
                            "category": "Nintendo 64",
                            "loose_price_cents": 2000,
                            "currency": "USD",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(pc_handler)),
        )

        response = service.detail("999")

        self.assertIsNone(response.result.imageUrl)
        self.assertEqual(len(lego_requests), 0)


class LegoSetNumberTest(unittest.TestCase):
    def test_extracts_number_after_hash(self) -> None:
        self.assertEqual(_lego_set_number("Altair #7322"), "7322")

    def test_strips_leading_zeros(self) -> None:
        self.assertEqual(_lego_set_number("Town Mini-Figures #0011"), "11")

    def test_returns_none_without_hash(self) -> None:
        self.assertIsNone(_lego_set_number("Altair"))


class MagicImageEnrichmentTest(unittest.TestCase):
    # scryfall_magic_catalog (imported from Scryfall's free bulk export --
    # see scripts/import_scryfall_magic_catalog.py) is our own Supabase
    # table, so this routes through the same `client` mock as every other
    # lookup, keyed off the request path/params. This enrichment only runs
    # in detail() now, so the row fetch goes through _fetch_catalog_row's
    # GET /rest/v1/pricecharting_catalog?pricecharting_id=eq.<id> shape,
    # not the search_pricecharting_catalog RPC.

    def _handler(self, *, search_row: dict, number_rows: dict, name_rows: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(200, json=[])
            if "scryfall_magic_catalog" in url:
                params = request.url.params
                set_name = params.get("normalized_set_name", "").removeprefix("eq.")
                if "collector_number" in params:
                    number = params.get("collector_number", "").removeprefix("eq.")
                    return httpx.Response(200, json=number_rows.get((set_name, number), []))
                if "normalized_name" in params:
                    name = params.get("normalized_name", "").removeprefix("eq.")
                    return httpx.Response(200, json=name_rows.get((set_name, name), []))
                return httpx.Response(200, json=[])
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(200, json=[search_row])
            return httpx.Response(200, json=[])

        return handler

    def test_detail_matches_by_collector_number(self) -> None:
        # Real production case: Scryfall's Gilded Showcase print of this
        # card has collector_number "365", matching PriceCharting's own
        # numbering exactly.
        search_row = {
            "pricecharting_id": "3773958",
            "product_name": "Cabaretti Charm [Gilded Foil] #365",
            "console_name": "Magic Streets of New Capenna",
            "category": "Magic Streets of New Capenna",
            "loose_price_cents": 5000,
            "currency": "USD",
        }
        number_rows = {
            ("streets of new capenna", "365"): [
                {"image_url": "https://cards.scryfall.io/normal/gilded-charm.jpg"},
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(search_row=search_row, number_rows=number_rows, name_rows={})
                )
            ),
        )

        response = service.detail("3773958")

        self.assertEqual(
            response.result.imageUrl, "https://cards.scryfall.io/normal/gilded-charm.jpg"
        )

    def test_detail_falls_back_to_name_when_no_number(self) -> None:
        search_row = {
            "pricecharting_id": "2244134",
            "product_name": "Angel of Mercy",
            "console_name": "Magic Starter 1999",
            "category": "Magic Starter 1999",
            "loose_price_cents": 200,
            "currency": "USD",
        }
        name_rows = {
            ("starter 1999", "angel of mercy"): [
                {"image_url": "https://cards.scryfall.io/normal/angel-of-mercy.jpg"},
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(search_row=search_row, number_rows={}, name_rows=name_rows)
                )
            ),
        )

        response = service.detail("2244134")

        self.assertEqual(
            response.result.imageUrl, "https://cards.scryfall.io/normal/angel-of-mercy.jpg"
        )

    def test_detail_suppresses_ambiguous_name_match(self) -> None:
        # More than one row for the same set+name (e.g. a reprinted basic
        # land with no distinguishing number) is never guessed at.
        search_row = {
            "pricecharting_id": "1",
            "product_name": "Forest",
            "console_name": "Magic Starter 1999",
            "category": "Magic Starter 1999",
            "loose_price_cents": 50,
            "currency": "USD",
        }
        name_rows = {
            ("starter 1999", "forest"): [
                {"image_url": "https://cards.scryfall.io/normal/forest-1.jpg"},
                {"image_url": "https://cards.scryfall.io/normal/forest-2.jpg"},
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(search_row=search_row, number_rows={}, name_rows=name_rows)
                )
            ),
        )

        response = service.detail("1")

        self.assertIsNone(response.result.imageUrl)

    def test_detail_skips_non_magic_set(self) -> None:
        magic_requests: list[httpx.Request] = []

        def pc_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "scryfall_magic_catalog" in url:
                magic_requests.append(request)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(200, json=[])
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "999",
                            "product_name": "Some Game",
                            "console_name": "Nintendo 64",
                            "category": "Nintendo 64",
                            "loose_price_cents": 2000,
                            "currency": "USD",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(pc_handler)),
        )

        response = service.detail("999")

        self.assertIsNone(response.result.imageUrl)
        self.assertEqual(len(magic_requests), 0)


class MagicTextHelpersTest(unittest.TestCase):
    def test_normalizes_apostrophes_and_punctuation(self) -> None:
        self.assertEqual(_normalize_magic_text("Urza's Saga"), "urzas saga")

    def test_extracts_card_number(self) -> None:
        self.assertEqual(_magic_card_number("Cabaretti Charm [Gilded Foil] #365"), "365")

    def test_returns_none_without_number(self) -> None:
        self.assertIsNone(_magic_card_number("Angel of Mercy"))

    def test_strips_bracket_and_number_from_card_name(self) -> None:
        self.assertEqual(
            _magic_card_name("Cabaretti Charm [Gilded Foil] #365"), "Cabaretti Charm"
        )


class YugiohImageEnrichmentTest(unittest.TestCase):
    # yugioh_catalog (imported from YGOPRODeck primary + TCGCSV fallback
    # -- see scripts/import_ygoprodeck_catalog.py and
    # scripts/import_tcgcsv_yugioh_catalog.py) is our own Supabase table,
    # so this routes through the same `client` mock as every other
    # lookup, keyed off the request path/params. This enrichment only
    # runs in detail() now, so the row fetch goes through
    # _fetch_catalog_row's GET /rest/v1/pricecharting_catalog?pricecharting_id=eq.<id>
    # shape, not the search_pricecharting_catalog RPC.

    def _handler(self, *, search_row: dict, code_rows: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(200, json=[])
            if "scryfall_magic_catalog" in url:
                return httpx.Response(200, json=[])
            if "yugioh_catalog" in url:
                code = request.url.params.get("set_code", "").removeprefix("eq.")
                return httpx.Response(200, json=code_rows.get(code, []))
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(200, json=[search_row])
            return httpx.Response(200, json=[])

        return handler

    def test_detail_matches_by_set_code(self) -> None:
        search_row = {
            "pricecharting_id": "1",
            "product_name": "Where Arf Thou? SD40-JP033",
            "console_name": "YuGiOh Japanese Structure Deck: Ice Barrier of the Frozen Prison",
            "category": "YuGiOh Japanese Structure Deck: Ice Barrier of the Frozen Prison",
            "loose_price_cents": 500,
            "currency": "USD",
        }
        code_rows = {
            "SD40-JP033": [
                {"image_url": "https://images.ygoprodeck.com/images/cards/12345.jpg"},
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(self._handler(search_row=search_row, code_rows=code_rows))
            ),
        )

        response = service.detail("1")

        self.assertEqual(
            response.result.imageUrl,
            "https://images.ygoprodeck.com/images/cards/12345.jpg",
        )

    def test_detail_skips_row_without_set_code(self) -> None:
        search_row = {
            "pricecharting_id": "2",
            "product_name": "Booster Pack",
            "console_name": "YuGiOh OTS Tournament Pack 14",
            "category": "YuGiOh OTS Tournament Pack 14",
            "loose_price_cents": 500,
            "currency": "USD",
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(self._handler(search_row=search_row, code_rows={}))
            ),
        )

        response = service.detail("2")

        self.assertIsNone(response.result.imageUrl)

    def test_detail_skips_non_yugioh_set(self) -> None:
        yugioh_requests: list[httpx.Request] = []

        def pc_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "yugioh_catalog" in url:
                yugioh_requests.append(request)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(200, json=[])
            if "scryfall_magic_catalog" in url:
                return httpx.Response(200, json=[])
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "999",
                            "product_name": "Some Game LOB-006",
                            "console_name": "Nintendo 64",
                            "category": "Nintendo 64",
                            "loose_price_cents": 2000,
                            "currency": "USD",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(pc_handler)),
        )

        response = service.detail("999")

        self.assertIsNone(response.result.imageUrl)
        self.assertEqual(len(yugioh_requests), 0)


class YugiohSetCodeTest(unittest.TestCase):
    def test_extracts_set_code(self) -> None:
        self.assertEqual(_yugioh_set_code("Where Arf Thou? SD40-JP033"), "SD40-JP033")

    def test_extracts_code_after_bracket_tag(self) -> None:
        self.assertEqual(
            _yugioh_set_code("Hieracosphinx [Ultimate Rare] TLM-JP012"), "TLM-JP012"
        )

    def test_returns_none_for_sealed_product(self) -> None:
        self.assertIsNone(_yugioh_set_code("Booster Pack"))


class LorcanaImageEnrichmentTest(unittest.TestCase):
    # lorcana_catalog (imported from lorcana-api.com primary + Lorcast
    # fallback -- see scripts/import_lorcana_api_catalog.py and
    # scripts/import_lorcast_catalog.py) is our own Supabase table, so
    # this routes through the same `client` mock as every other lookup,
    # keyed off the request path/params. This enrichment only runs in
    # detail() now, so the row fetch goes through _fetch_catalog_row's
    # GET /rest/v1/pricecharting_catalog?pricecharting_id=eq.<id> shape,
    # not the search_pricecharting_catalog RPC.

    def _handler(self, *, search_row: dict, catalog_rows: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(200, json=[])
            if "scryfall_magic_catalog" in url:
                return httpx.Response(200, json=[])
            if "yugioh_catalog" in url:
                return httpx.Response(200, json=[])
            if "lorcana_catalog" in url:
                params = request.url.params
                set_name = params.get("normalized_set_name", "").removeprefix("eq.")
                number = params.get("card_number", "").removeprefix("eq.")
                return httpx.Response(200, json=catalog_rows.get((set_name, number), []))
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(200, json=[search_row])
            return httpx.Response(200, json=[])

        return handler

    def test_detail_matches_by_set_and_number(self) -> None:
        search_row = {
            "pricecharting_id": "1",
            "product_name": "Ink Geyser [Foil] #119",
            "console_name": "Lorcana Archazia's Island",
            "category": "Lorcana Archazia's Island",
            "loose_price_cents": 200,
            "currency": "USD",
        }
        catalog_rows = {
            ("archazias island", "119"): [
                {"image_url": "https://cards.lorcast.io/card/digital/normal/ink-geyser.avif"},
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(search_row=search_row, catalog_rows=catalog_rows)
                )
            ),
        )

        response = service.detail("1")

        self.assertEqual(
            response.result.imageUrl,
            "https://cards.lorcast.io/card/digital/normal/ink-geyser.avif",
        )

    def test_detail_normalizes_punctuation_in_set_name(self) -> None:
        # Real production case: PriceCharting's "Lorcana Attack of the
        # Vine" vs Lorcast/lorcana-api.com's "Attack of the Vine!" --
        # must still match after normalization strips the "!".
        search_row = {
            "pricecharting_id": "2",
            "product_name": "Broken Pod #70",
            "console_name": "Lorcana Attack of the Vine",
            "category": "Lorcana Attack of the Vine",
            "loose_price_cents": 100,
            "currency": "USD",
        }
        catalog_rows = {
            ("attack of the vine", "70"): [
                {"image_url": "https://api.lorcana.ravensburger.com/images/en/set13/70.jpg"},
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(search_row=search_row, catalog_rows=catalog_rows)
                )
            ),
        )

        response = service.detail("2")

        self.assertEqual(
            response.result.imageUrl,
            "https://api.lorcana.ravensburger.com/images/en/set13/70.jpg",
        )

    def test_detail_skips_non_lorcana_set(self) -> None:
        lorcana_requests: list[httpx.Request] = []

        def pc_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "lorcana_catalog" in url:
                lorcana_requests.append(request)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(200, json=[])
            if "scryfall_magic_catalog" in url:
                return httpx.Response(200, json=[])
            if "yugioh_catalog" in url:
                return httpx.Response(200, json=[])
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "999",
                            "product_name": "Some Game #70",
                            "console_name": "Nintendo 64",
                            "category": "Nintendo 64",
                            "loose_price_cents": 2000,
                            "currency": "USD",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(pc_handler)),
        )

        response = service.detail("999")

        self.assertIsNone(response.result.imageUrl)
        self.assertEqual(len(lorcana_requests), 0)

class LorcanaSetNameTest(unittest.TestCase):
    def test_strips_lorcana_prefix(self) -> None:
        self.assertEqual(
            _lorcana_set_name_from_console("Lorcana Attack of the Vine"), "Attack of the Vine"
        )

    def test_returns_none_for_non_lorcana_console(self) -> None:
        self.assertIsNone(_lorcana_set_name_from_console("Pokemon Base Set"))


class OnePieceImageEnrichmentTest(unittest.TestCase):
    # one_piece_catalog (imported from optcgapi.com -- see
    # scripts/import_onepiece_catalog.py) is our own Supabase table, so
    # this routes through the same `client` mock as every other lookup,
    # keyed off the request path/params. This enrichment only runs in
    # detail() now, so the row fetch goes through _fetch_catalog_row's
    # GET /rest/v1/pricecharting_catalog?pricecharting_id=eq.<id> shape,
    # not the search_pricecharting_catalog RPC.

    def _handler(self, *, search_row: dict, code_rows: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(200, json=[])
            if "scryfall_magic_catalog" in url:
                return httpx.Response(200, json=[])
            if "yugioh_catalog" in url:
                return httpx.Response(200, json=[])
            if "lorcana_catalog" in url:
                return httpx.Response(200, json=[])
            if "one_piece_catalog" in url:
                code = request.url.params.get("card_set_id", "").removeprefix("eq.")
                return httpx.Response(200, json=code_rows.get(code, []))
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(200, json=[search_row])
            return httpx.Response(200, json=[])

        return handler

    def test_detail_matches_unambiguous_plain_row(self) -> None:
        search_row = {
            "pricecharting_id": "1",
            "product_name": "Captain John OP07-082",
            "console_name": "One Piece 500 Years in the Future",
            "category": "One Piece 500 Years in the Future",
            "loose_price_cents": 100,
            "currency": "USD",
        }
        code_rows = {
            "OP07-082": [
                {
                    "card_name": "Captain John",
                    "is_plain": True,
                    "image_url": "https://optcgapi.com/media/static/Card_Images/OP07-082.jpg",
                },
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(search_row=search_row, code_rows=code_rows)
                )
            ),
        )

        response = service.detail("1")

        self.assertEqual(
            response.result.imageUrl,
            "https://optcgapi.com/media/static/Card_Images/OP07-082.jpg",
        )

    def test_detail_suppresses_plain_row_when_multiple_plain_entries_exist(self) -> None:
        # Real production case: optcgapi.com's own data has cards with
        # TWO identical, indistinguishable "plain" entries for the same
        # code (e.g. real "Izo" OP01-033 -- an unlabeled reprint sharing
        # both code and name with the original) -- must not guess which
        # one a plain, untagged PriceCharting row refers to.
        search_row = {
            "pricecharting_id": "2",
            "product_name": "Izo OP01-033",
            "console_name": "One Piece Romance Dawn",
            "category": "One Piece Romance Dawn",
            "loose_price_cents": 50,
            "currency": "USD",
        }
        code_rows = {
            "OP01-033": [
                {
                    "card_name": "Izo",
                    "is_plain": True,
                    "image_url": "https://optcgapi.com/media/static/Card_Images/OP01-033.jpg",
                },
                {
                    "card_name": "Izo",
                    "is_plain": True,
                    "image_url": "https://optcgapi.com/media/static/Card_Images/OP01-033_r1.jpg",
                },
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(search_row=search_row, code_rows=code_rows)
                )
            ),
        )

        response = service.detail("2")

        self.assertIsNone(response.result.imageUrl)

    def test_detail_matches_exact_variant_by_word_subset(self) -> None:
        search_row = {
            "pricecharting_id": "3",
            "product_name": "Perona [Box Topper] OP01-077",
            "console_name": "One Piece Romance Dawn",
            "category": "One Piece Romance Dawn",
            "loose_price_cents": 500,
            "currency": "USD",
        }
        code_rows = {
            "OP01-077": [
                {
                    "card_name": "Perona",
                    "is_plain": True,
                    "image_url": "https://optcgapi.com/media/static/Card_Images/OP01-077.jpg",
                },
                {
                    "card_name": "Perona (Box Topper)",
                    "is_plain": False,
                    "image_url": "https://optcgapi.com/media/static/Card_Images/OP01-077_p1.jpg",
                },
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(search_row=search_row, code_rows=code_rows)
                )
            ),
        )

        response = service.detail("3")

        self.assertEqual(
            response.result.imageUrl,
            "https://optcgapi.com/media/static/Card_Images/OP01-077_p1.jpg",
        )

    def test_detail_does_not_cross_match_similarly_named_variants(self) -> None:
        # Real production bug, caught live: "[Championship 2024 Top
        # Player]" and "[Championship 2024 Finalist]" share the words
        # "championship"/"2024" -- an earlier "any word in common"
        # matching rule matched both to the SAME (wrong, for one of them)
        # candidate. Requiring every tag word to be a subset of the
        # candidate's name, and requiring that subset match to be unique,
        # fixes this: "Top Player" only fully matches the "Top Player
        # Pack" candidate.
        search_row = {
            "pricecharting_id": "4",
            "product_name": "Perona [Championship 2024 Top Player] OP01-077",
            "console_name": "One Piece Romance Dawn",
            "category": "One Piece Romance Dawn",
            "loose_price_cents": 500,
            "currency": "USD",
        }
        code_rows = {
            "OP01-077": [
                {
                    "card_name": "Perona",
                    "is_plain": True,
                    "image_url": "https://optcgapi.com/media/static/Card_Images/OP01-077.jpg",
                },
                {
                    "card_name": "Perona (Championship 2024 Finalist Card Set)",
                    "is_plain": False,
                    "image_url": "https://optcgapi.com/media/static/Card_Images/finalist.jpg",
                },
                {
                    "card_name": "Perona (Championship 2024 Top Player Pack)",
                    "is_plain": False,
                    "image_url": "https://optcgapi.com/media/static/Card_Images/top_player.jpg",
                },
            ],
        }

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    self._handler(search_row=search_row, code_rows=code_rows)
                )
            ),
        )

        response = service.detail("4")

        self.assertEqual(
            response.result.imageUrl,
            "https://optcgapi.com/media/static/Card_Images/top_player.jpg",
        )

    def test_detail_skips_non_onepiece_set(self) -> None:
        onepiece_requests: list[httpx.Request] = []

        def pc_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "one_piece_catalog" in url:
                onepiece_requests.append(request)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(200, json=[])
            if "scryfall_magic_catalog" in url:
                return httpx.Response(200, json=[])
            if "yugioh_catalog" in url:
                return httpx.Response(200, json=[])
            if "lorcana_catalog" in url:
                return httpx.Response(200, json=[])
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "999",
                            "product_name": "Some Game OP07-082",
                            "console_name": "Nintendo 64",
                            "category": "Nintendo 64",
                            "loose_price_cents": 2000,
                            "currency": "USD",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(pc_handler)),
        )

        response = service.detail("999")

        self.assertIsNone(response.result.imageUrl)
        self.assertEqual(len(onepiece_requests), 0)


class OnePieceSetCodeTest(unittest.TestCase):
    def test_extracts_code(self) -> None:
        self.assertEqual(_onepiece_set_code("Captain John OP07-082"), "OP07-082")

    def test_extracts_single_letter_promo_code(self) -> None:
        self.assertEqual(_onepiece_set_code("Bartolomeo [Promotion Pack] P-029"), "P-029")

    def test_returns_none_without_code(self) -> None:
        self.assertIsNone(_onepiece_set_code("DON!! Card [Nico Robin]"))


class PokemonCardNumberTest(unittest.TestCase):
    def test_extracts_number_after_hash(self) -> None:
        self.assertEqual(_pokemon_card_number("Charizard [1999-2000] #4"), "4")

    def test_returns_none_without_hash(self) -> None:
        self.assertIsNone(_pokemon_card_number("Charizard"))


class FunkoLookupTitleTest(unittest.TestCase):
    def test_strips_figure_number(self) -> None:
        self.assertEqual(
            _funko_lookup_title("13th Battalion Trooper #645"), "13th battalion trooper"
        )

    def test_strips_bracket_tag_and_year_and_number(self) -> None:
        self.assertEqual(
            _funko_lookup_title("Guardians Of The Galaxy [Funko Pop] #12 (2019)"),
            "guardians of the galaxy",
        )

    def test_normalizes_smart_quotes_and_case(self) -> None:
        self.assertEqual(
            _funko_lookup_title("“The American Nightmare” Cody Rhodes #198"),
            '"the american nightmare" cody rhodes',
        )

    def test_passthrough_when_no_special_tokens(self) -> None:
        self.assertEqual(_funko_lookup_title("12th Man Freddy Funko"), "12th man freddy funko")


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
