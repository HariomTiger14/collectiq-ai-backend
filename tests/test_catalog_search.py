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
    _pokemon_variant_token,
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

    def test_search_enriches_funko_result_with_real_image(self) -> None:
        # PriceCharting has real pricing for Funko Pop rows but no image
        # field at all (confirmed live, zero image data in raw_payload for
        # any category). funko_pop_catalog is a static reference table
        # (imported from the open-source funko-pop-data dataset) used only
        # to attach a real photo when a confident exact-title match exists.
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
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
        self.assertEqual(
            response.results[0].imageUrl,
            "https://images.hobbydb.com/1950-batmobile.png",
        )
        self.assertTrue(
            any("funko_pop_catalog" in str(r.url) for r in requests)
        )

    def test_search_leaves_funko_result_unenriched_when_no_match(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])  # no match found
            if "search_pricecharting_catalog" in url:
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

        response = service.search("some obscure figure", limit=10)

        self.assertIsNone(response.results[0].imageUrl)

    def test_search_skips_funko_lookup_for_non_funko_results(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "search_pricecharting_catalog" in url:
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

        service.search("charizard", limit=10)

        self.assertFalse(
            any("funko_pop_catalog" in str(r.url) for r in requests)
        )


class PokemonImageEnrichmentTest(unittest.TestCase):
    # tcgplayer_pokemon_catalog (imported from TCGCSV — see
    # scripts/import_tcgplayer_pokemon_catalog.py) is our own Supabase
    # table, so every scenario below routes through the same `client`
    # mock as the pricecharting_catalog/funko lookups, keyed off the
    # request path/method rather than a separate external client.

    def _handler(self, *, search_row: dict, tcgplayer_rows: dict, sibling_rows: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "search_pricecharting_catalog" in url:
                return httpx.Response(200, json=[search_row])
            if "tcgplayer_pokemon_catalog" in url:
                group_name = request.url.params.get("group_name", "").removeprefix("eq.")
                card_number = request.url.params.get("card_number", "").removeprefix("eq.")
                return httpx.Response(
                    200, json=tcgplayer_rows.get((group_name, card_number), [])
                )
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                console_name = request.url.params.get("console_name", "").removeprefix("eq.")
                return httpx.Response(200, json=sibling_rows.get(console_name, []))
            return httpx.Response(200, json=[])

        return handler

    def test_search_uses_plain_image_when_no_sibling_variant_rows(self) -> None:
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

        response = service.search("charizard base set", limit=10)

        self.assertEqual(
            response.results[0].imageUrl,
            "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
        )

    def test_search_suppresses_generic_image_when_sibling_variant_rows_exist(self) -> None:
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

        response = service.search("charizard base set", limit=10)

        self.assertIsNone(response.results[0].imageUrl)

    def test_search_uses_exact_shadowless_image(self) -> None:
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

        response = service.search("charizard shadowless", limit=10)

        self.assertEqual(
            response.results[0].imageUrl,
            "https://tcgplayer-cdn.tcgplayer.com/product/106999_200w.jpg",
        )

    def test_search_uses_exact_error_variant_image(self) -> None:
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

        response = service.search("charizard black dot error", limit=10)

        self.assertEqual(
            response.results[0].imageUrl,
            "https://tcgplayer-cdn.tcgplayer.com/product/657516_200w.jpg",
        )

    def test_search_skips_unmapped_pokemon_set(self) -> None:
        tcgplayer_requests: list[httpx.Request] = []

        def pc_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "tcgplayer_pokemon_catalog" in url:
                tcgplayer_requests.append(request)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "search_pricecharting_catalog" in url:
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

        response = service.search("charizard expedition", limit=10)

        self.assertIsNone(response.results[0].imageUrl)
        self.assertEqual(len(tcgplayer_requests), 0)


class PokemonVariantTokenTest(unittest.TestCase):
    def test_extracts_bracket_tag(self) -> None:
        self.assertEqual(_pokemon_variant_token("Charizard [Shadowless] #4"), "shadowless")

    def test_returns_none_without_brackets(self) -> None:
        self.assertIsNone(_pokemon_variant_token("Charizard #4"))


class LegoImageEnrichmentTest(unittest.TestCase):
    # rebrickable_lego_catalog (imported from Rebrickable's free bulk
    # export -- see scripts/import_rebrickable_lego_catalog.py) is our own
    # Supabase table, so this routes through the same `client` mock as
    # every other lookup, keyed off the request path.

    def _handler(self, *, search_row: dict, lego_rows: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                base_number = request.url.params.get("base_number", "").removeprefix("eq.")
                return httpx.Response(200, json=lego_rows.get(base_number, []))
            if "search_pricecharting_catalog" in url:
                return httpx.Response(200, json=[search_row])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        return handler

    def test_search_uses_image_when_title_words_overlap(self) -> None:
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

        response = service.search("altair lego", limit=10)

        self.assertEqual(
            response.results[0].imageUrl,
            "https://cdn.rebrickable.com/media/sets/7322-1.jpg",
        )

    def test_search_rejects_number_match_without_word_overlap(self) -> None:
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

        response = service.search("roof bricks lego", limit=10)

        self.assertIsNone(response.results[0].imageUrl)

    def test_search_skips_non_lego_set(self) -> None:
        lego_requests: list[httpx.Request] = []

        def pc_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "rebrickable_lego_catalog" in url:
                lego_requests.append(request)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "search_pricecharting_catalog" in url:
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

        response = service.search("some game", limit=10)

        self.assertIsNone(response.results[0].imageUrl)
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
    # lookup, keyed off the request path/params.

    def _handler(self, *, search_row: dict, number_rows: dict, name_rows: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
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
            if "search_pricecharting_catalog" in url:
                return httpx.Response(200, json=[search_row])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        return handler

    def test_search_matches_by_collector_number(self) -> None:
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

        response = service.search("cabaretti charm gilded", limit=10)

        self.assertEqual(
            response.results[0].imageUrl, "https://cards.scryfall.io/normal/gilded-charm.jpg"
        )

    def test_search_falls_back_to_name_when_no_number(self) -> None:
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

        response = service.search("angel of mercy", limit=10)

        self.assertEqual(
            response.results[0].imageUrl, "https://cards.scryfall.io/normal/angel-of-mercy.jpg"
        )

    def test_search_suppresses_ambiguous_name_match(self) -> None:
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

        response = service.search("forest", limit=10)

        self.assertIsNone(response.results[0].imageUrl)

    def test_search_skips_non_magic_set(self) -> None:
        magic_requests: list[httpx.Request] = []

        def pc_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "scryfall_magic_catalog" in url:
                magic_requests.append(request)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "tcgplayer_pokemon_catalog" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(200, json=[])
            if "search_pricecharting_catalog" in url:
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

        response = service.search("some game", limit=10)

        self.assertIsNone(response.results[0].imageUrl)
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
