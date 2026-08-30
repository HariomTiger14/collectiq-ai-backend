import json
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.pricing.catalog_search_service import (
    resolve_category_group_filters,
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
    _video_game_prefix_suffix_is_safe,
    _video_game_strip_edition_suffix,
    _video_game_strip_punctuation,
    _yugioh_set_code,
)



@contextmanager
def _patched_marketplace_credentials():
    # These detail() tests drive the real eBay/PriceCharting listing
    # services through the injected MockTransport client, but both refuse
    # to run without credentials -- inject fake ones so the tests never
    # depend on the developer's real environment (they silently skipped
    # the providers and failed wherever EBAY_CLIENT_ID/SECRET or
    # PRICECHARTING_API_KEY were unset). Settings is a frozen dataclass,
    # so swap each module's reference for a modified copy.
    creds = replace(
        settings,
        ebay_client_id="test-ebay-client-id",
        ebay_client_secret="test-ebay-client-secret",
        pricecharting_api_key="test-pricecharting-key",
    )
    with patch(
        "app.services.pricing.ebay_listing_service.settings", creds
    ), patch(
        "app.services.pricing.pricecharting_listing_service.settings", creds
    ):
        yield


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

    def test_search_forwards_category_group_and_price_filters_to_pricecharting_rpc(
        self,
    ) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "search_pricecharting_catalog" in str(request.url):
                captured["json"] = json.loads(request.read())
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        service.search("card", limit=10, category_group="coins", min_price=5, max_price=50)

        self.assertEqual(captured["json"]["category_keywords"], ["Coin"])
        self.assertEqual(captured["json"]["min_price_cents"], 500)
        self.assertEqual(captured["json"]["max_price_cents"], 5000)
        self.assertIsNone(captured["json"]["platform_group_filter"])

    def test_search_forwards_platform_group_to_pricecharting_rpc(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "search_pricecharting_catalog" in str(request.url):
                captured["json"] = json.loads(request.read())
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        service.search("mario", limit=10, category_group="nintendo")

        self.assertIsNone(captured["json"]["category_keywords"])
        self.assertEqual(captured["json"]["platform_group_filter"], "nintendo")

    def test_search_forwards_sports_cards_subcategory_to_pricecharting_rpc(
        self,
    ) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "search_pricecharting_catalog" in str(request.url):
                captured["json"] = json.loads(request.read())
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        service.search(
            "trout", limit=10, category_group="sports-cards", subcategory="baseball"
        )

        self.assertEqual(captured["json"]["category_keywords"], ["Baseball"])
        self.assertIsNone(captured["json"]["platform_group_filter"])

    def test_search_ignores_unknown_subcategory_for_sports_cards(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "search_pricecharting_catalog" in str(request.url):
                captured["json"] = json.loads(request.read())
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        # An unrecognized subcategory falls back to the full category's
        # combined keyword list, same as no subcategory at all.
        service.search(
            "trout", limit=10, category_group="sports-cards", subcategory="cricket"
        )

        self.assertEqual(
            captured["json"]["category_keywords"],
            ["Baseball", "Basketball", "Football", "Hockey", "Soccer"],
        )

    def test_search_video_games_category_with_platform_subcategory(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "search_pricecharting_catalog" in str(request.url):
                captured["json"] = json.loads(request.read())
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        service.search(
            "mario", limit=10, category_group="video-games", subcategory="nintendo"
        )

        self.assertIsNone(captured["json"]["category_keywords"])
        self.assertEqual(captured["json"]["platform_group_filter"], "nintendo")

    def test_search_video_games_category_with_no_subcategory_uses_any_platform_sentinel(
        self,
    ) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "search_pricecharting_catalog" in str(request.url):
                captured["json"] = json.loads(request.read())
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        service.search("mario", limit=10, category_group="video-games")

        self.assertIsNone(captured["json"]["category_keywords"])
        self.assertEqual(captured["json"]["platform_group_filter"], "__any_platform__")

    def test_search_forwards_price_filters_to_kicksdb_rpc(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "search_kicksdb_catalog" in str(request.url):
                captured["json"] = json.loads(request.read())
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        service.search("jordan", limit=10, min_price=20, max_price=200)

        self.assertEqual(captured["json"]["min_price_cents"], 2000)
        self.assertEqual(captured["json"]["max_price_cents"], 20000)

    def test_search_source_pricecharting_skips_kicksdb_fetch_entirely(self) -> None:
        kicksdb_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "search_kicksdb_catalog" in str(request.url):
                kicksdb_requests.append(request)
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        service.search("card", limit=10, source="pricecharting")

        self.assertEqual(kicksdb_requests, [])

    def test_search_source_kicksdb_skips_pricecharting_fetch_entirely(self) -> None:
        pricecharting_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "search_pricecharting_catalog" in str(request.url):
                pricecharting_requests.append(request)
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        service.search("jordan", limit=10, source="kicksdb")

        self.assertEqual(pricecharting_requests, [])

    def _source_probe(self) -> tuple[CatalogSearchService, list[str], list[str]]:
        pricecharting: list[str] = []
        kicksdb: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                kicksdb.append(url)
            elif "search_pricecharting_catalog" in url:
                pricecharting.append(url)
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        return service, pricecharting, kicksdb

    def test_category_filter_excludes_kicksdb_which_has_no_such_taxonomy(
        self,
    ) -> None:
        # KicksDB rows carry no category/platform group, so they can never
        # satisfy a PriceCharting category filter -- fetching them anyway
        # let a Yu-Gi-Oh-filtered search return "Nike Air Max Muscle 95
        # Yu-Gi-Oh! Joey" alongside real cards.
        service, pricecharting, kicksdb = self._source_probe()

        service.search(
            "yu-gi-oh",
            limit=10,
            category_group="trading-card-games",
            subcategory="yugioh",
        )

        self.assertEqual(kicksdb, [])
        self.assertEqual(len(pricecharting), 1)

    def test_sneakers_category_filter_queries_only_kicksdb(self) -> None:
        # Sneakers live entirely in kicksdb_catalog, so resolving them
        # through the PriceCharting taxonomy would return nothing at all.
        service, pricecharting, kicksdb = self._source_probe()

        service.search("sneakers", limit=10, category_group="sneakers")

        self.assertEqual(pricecharting, [])
        self.assertEqual(len(kicksdb), 1)

    def test_a_category_filter_alone_browses_without_a_search_term(self) -> None:
        # "Show me this category" is a complete request. Requiring a query
        # forced Discover's chips to type a representative term into the
        # search box just to get results, which read as the app typing for
        # the user.
        service, pricecharting, _ = self._source_probe()

        response = service.search("", limit=10, category_group="comics")

        self.assertEqual(len(pricecharting), 1, "browse should still query")
        self.assertEqual(response.query, "")

    def test_a_short_query_with_no_category_filter_still_returns_nothing(
        self,
    ) -> None:
        # Unchanged: a stray one-character keystroke must not sweep the
        # whole catalog.
        service, pricecharting, kicksdb = self._source_probe()

        response = service.search("a", limit=10)

        self.assertEqual(response.count, 0)
        self.assertEqual(pricecharting, [])
        self.assertEqual(kicksdb, [])

    def test_one_piece_is_part_of_the_trading_card_games_group(self) -> None:
        # It was missing from the group despite thousands of its cards being
        # in the catalog, so filtering to trading cards EXCLUDED the game:
        # "Luffy" fell through to "Fluffy Berry" Pokemon cards and the set
        # code "OP01" returned YuGiOh cards.
        keywords, platform = resolve_category_group_filters(
            "trading-card-games", "onepiece",
        )
        self.assertEqual(keywords, ["One Piece"])
        self.assertIsNone(platform)

        all_keywords, _ = resolve_category_group_filters("trading-card-games")
        self.assertIn("One Piece", all_keywords)

    def test_unfiltered_search_still_queries_both_sources(self) -> None:
        service, pricecharting, kicksdb = self._source_probe()

        service.search("charizard", limit=10)

        self.assertEqual(len(pricecharting), 1)
        self.assertEqual(len(kicksdb), 1)

    def test_explicit_source_still_wins_over_the_category_filter(self) -> None:
        service, pricecharting, kicksdb = self._source_probe()

        service.search(
            "jordan", limit=10, category_group="trading-card-games", source="kicksdb",
        )

        self.assertEqual(pricecharting, [])
        self.assertEqual(len(kicksdb), 1)

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

    def test_search_breaks_score_ties_by_popularity_not_alphabet(self) -> None:
        # A broad query like "nike" scores every "Nike ..." title
        # identically; the tie must go to the marketplace-popular shoe
        # (lower KicksDB rank), not the alphabetically-first one. This is
        # the "searching nike showed only A'ja Wilson women's models"
        # bug: alphabetical tie-breaking front-loaded the A's.
        def kicksdb_row(kicksdb_id: str, title: str, rank: int | None) -> dict:
            return {
                "kicksdb_id": kicksdb_id,
                "title": title,
                "brand": "Nike",
                "model": title,
                "category": "Sneakers",
                "rank": rank,
                "currency": "USD",
                "min_price_cents": 10000,
                "max_price_cents": 20000,
                "avg_price_cents": 15000,
                "sku": kicksdb_id,
                "updated_at": "2026-08-10T00:00:00Z",
            }

        def handler(request: httpx.Request) -> httpx.Response:
            if "search_kicksdb_catalog" in str(request.url):
                return httpx.Response(
                    200,
                    json=[
                        kicksdb_row("kdb-aja", "Nike A'ja Wilson A'One", rank=180000),
                        kicksdb_row("kdb-af1", "Nike Air Force 1 Low White", rank=20),
                        kicksdb_row("kdb-norank", "Nike Zoom Obscure", rank=None),
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("nike", limit=10)

        self.assertEqual(
            [result.id for result in response.results],
            ["kdb-af1", "kdb-aja", "kdb-norank"],
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

    def _kicksdb_detail_service_with_variants(
        self, variants: list[dict]
    ) -> CatalogSearchService:
        def handler(request: httpx.Request) -> httpx.Response:
            if "kicksdb_catalog_history" in str(request.url):
                return httpx.Response(200, json=[])
            if "kicksdb_catalog" in str(request.url):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "kicksdb_id": "kdb-9",
                            "title": "Air Jordan 4 Retro Infrared",
                            "brand": "Jordan",
                            "category": "Sneakers",
                            "currency": "USD",
                            "min_price_cents": 11500,
                            "max_price_cents": 39300,
                            "avg_price_cents": 18000,
                            "product_url": "https://stockx.com/air-jordan-4-infrared",
                            "sku": "DH6927-061",
                            "variants": variants,
                            "updated_at": "2026-08-10T00:00:00Z",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        return CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_kicksdb_detail_includes_per_size_marketplace_listings(self) -> None:
        # The variants JSON stored on the kicksdb_catalog row carries live
        # StockX market depth per size; detail() must surface it as
        # marketplaceListings without any extra request. Hidden and
        # zero-ask sizes are dead listings and must be skipped, and output
        # order follows the variant position, not JSON order.
        service = self._kicksdb_detail_service_with_variants(
            [
                {
                    "size": "10.5",
                    "size_type": "us m",
                    "sizes": [{"size": "US M 10.5", "type": "us m"}],
                    "position": 2,
                    "hidden": False,
                    "currency": "USD",
                    "lowest_ask": 131.0,
                    "total_asks": 4,
                    "sales_count_30_days": 2,
                },
                {
                    "size": "10",
                    "size_type": "us m",
                    "sizes": [{"size": "US M 10", "type": "us m"}],
                    "position": 1,
                    "hidden": False,
                    "currency": "USD",
                    "lowest_ask": 115,
                    "total_asks": 13,
                    "sales_count_30_days": 8,
                },
                {
                    "size": "9",
                    "size_type": "us m",
                    "sizes": [],
                    "position": 0,
                    "hidden": False,
                    "currency": "USD",
                    "lowest_ask": 0,
                    "total_asks": 0,
                    "sales_count_30_days": 0,
                },
                {
                    "size": "8",
                    "size_type": "us m",
                    "sizes": [{"size": "US M 8", "type": "us m"}],
                    "position": 3,
                    "hidden": True,
                    "currency": "USD",
                    "lowest_ask": 99,
                    "total_asks": 1,
                    "sales_count_30_days": 1,
                },
            ]
        )

        response = service.detail("kdb-9")

        self.assertEqual(len(response.marketplaceListings), 2)
        first, second = response.marketplaceListings
        self.assertEqual(first.title, "Size US M 10")
        self.assertEqual(first.size, "US M 10")
        self.assertEqual(first.price, 115.0)
        self.assertEqual(first.currency, "USD")
        self.assertEqual(first.source, "StockX")
        self.assertEqual(first.condition, "New")
        self.assertEqual(first.totalAsks, 13)
        self.assertEqual(first.salesLast30Days, 8)
        self.assertEqual(first.url, "https://stockx.com/air-jordan-4-infrared")
        self.assertEqual(second.size, "US M 10.5")
        self.assertEqual(second.price, 131.0)

    def test_kicksdb_marketplace_listings_convert_to_requested_currency(self) -> None:
        from app.services.pricing.catalog_search_service import _exchange_rate

        service = self._kicksdb_detail_service_with_variants(
            [
                {
                    "size": "10",
                    "size_type": "us m",
                    "sizes": [{"size": "US M 10", "type": "us m"}],
                    "position": 0,
                    "hidden": False,
                    "currency": "USD",
                    "lowest_ask": 115,
                    "total_asks": 13,
                    "sales_count_30_days": 8,
                },
            ]
        )

        response = service.detail("kdb-9", currency="AUD")

        listing = response.marketplaceListings[0]
        self.assertEqual(listing.currency, "AUD")
        self.assertEqual(listing.price, round(115 * _exchange_rate("USD", "AUD"), 2))

    def test_kicksdb_detail_without_variants_returns_no_listings(self) -> None:
        service = self._kicksdb_detail_service_with_variants([])

        response = service.detail("kdb-9")

        self.assertEqual(response.marketplaceListings, [])

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

    def test_detail_converts_pricing_and_history_when_currency_requested(self) -> None:
        # PriceCharting data is always USD-sourced -- requesting a non-USD
        # display currency must convert both the headline pricing AND every
        # history point consistently, using the same static FX rate
        # (settings.fx_usd_to_aud, default 1.52) currency_conversion.py
        # already uses for scan pricing. Without this, the chart and the
        # headline price would show different currencies on the same
        # screen.
        def handler(request: httpx.Request) -> httpx.Response:
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
                            "graded_price_cents": 80000,
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

        response = service.detail("999", history_limit=10, currency="AUD")

        self.assertEqual(response.result.pricing.currency, "AUD")
        self.assertEqual(response.result.pricing.originalCurrency, "USD")
        self.assertEqual(response.result.pricing.marketValue, round(161 * 1.52, 2))
        self.assertEqual(response.history[0].pricing.currency, "AUD")
        self.assertEqual(response.history[0].pricing.highEstimate, round(800 * 1.52, 2))

    def test_detail_does_not_convert_when_no_currency_requested(self) -> None:
        # Backward compatibility: omitting the currency param (existing
        # callers, and the admin catalog browse table which never sends
        # one) must behave exactly as before -- no conversion, no
        # originalCurrency stamped.
        def handler(request: httpx.Request) -> httpx.Response:
            if "pricecharting_catalog_history" in str(request.url):
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
                        "currency": "USD",
                        "normalized_identity": "charizard #4 base set pokemon cards",
                    }
                ],
            )

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.detail("999")

        self.assertEqual(response.result.pricing.currency, "USD")
        self.assertIsNone(response.result.pricing.originalCurrency)
        self.assertEqual(response.result.pricing.marketValue, 161)

    def test_detail_includes_ebay_listings_on_cache_miss(self) -> None:
        # Cache miss -> live eBay fetch (OAuth token, then Browse API
        # search) -> the fresh result is both returned AND written back to
        # ebay_listing_cache for next time.
        write_payloads: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if "catalog_marketplace_source_flags" in url:
                return httpx.Response(200, json=[{"source": "ebay", "enabled": True}])
            if "ebay_listing_cache" in url and request.method == "GET":
                return httpx.Response(200, json=[])  # cache miss
            if "ebay_listing_cache" in url and request.method == "POST":
                write_payloads.append(request)
                return httpx.Response(201, json=None)
            if "identity/v1/oauth2/token" in url:
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 7200})
            if "buy/browse/v1/item_summary/search" in url:
                return httpx.Response(
                    200,
                    json={
                        "itemSummaries": [
                            {
                                "title": "God of War PS4 Brand New",
                                "price": {"value": "21.49", "currency": "AUD"},
                                "condition": "New",
                                "itemWebUrl": "https://www.ebay.com/itm/12345",
                            },
                        ]
                    },
                )
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "45800",
                            "product_name": "God of War",
                            "console_name": "Playstation 4",
                            "category": "Action & Adventure",
                            "loose_price_cents": 1299,
                            "currency": "USD",
                            "normalized_identity": "god of war playstation 4",
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with _patched_marketplace_credentials():
            response = service.detail("45800", currency="AUD")

        self.assertEqual(len(response.marketplaceListings), 1)
        listing = response.marketplaceListings[0]
        self.assertEqual(listing.title, "God of War PS4 Brand New")
        self.assertEqual(listing.price, 21.49)
        self.assertEqual(listing.currency, "AUD")
        self.assertEqual(listing.url, "https://www.ebay.com/itm/12345")
        self.assertEqual(len(write_payloads), 1)

    def test_detail_fetches_usd_ebay_listings_when_no_currency_requested(self) -> None:
        # Regression: when the caller omits currency, pricing/history stay
        # in their raw USD source currency (no conversion). The eBay
        # marketplace picked for listings must match that -- EBAY_US, not
        # the AU-flavored fallback -- otherwise the response shows a USD
        # headline price alongside AUD "where to buy" listings.
        requested_marketplace_ids: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if "catalog_marketplace_source_flags" in url:
                return httpx.Response(200, json=[{"source": "ebay", "enabled": True}])
            if "ebay_listing_cache" in url and request.method == "GET":
                return httpx.Response(200, json=[])  # cache miss
            if "ebay_listing_cache" in url and request.method == "POST":
                return httpx.Response(201, json=None)
            if "identity/v1/oauth2/token" in url:
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 7200})
            if "buy/browse/v1/item_summary/search" in url:
                requested_marketplace_ids.append(request.headers.get("X-EBAY-C-MARKETPLACE-ID", ""))
                return httpx.Response(
                    200,
                    json={
                        "itemSummaries": [
                            {
                                "title": "God of War PS4 Brand New",
                                "price": {"value": "12.99", "currency": "USD"},
                                "condition": "New",
                                "itemWebUrl": "https://www.ebay.com/itm/12345",
                            },
                        ]
                    },
                )
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "45800",
                            "product_name": "God of War",
                            "console_name": "Playstation 4",
                            "category": "Action & Adventure",
                            "loose_price_cents": 1299,
                            "currency": "USD",
                            "normalized_identity": "god of war playstation 4",
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with _patched_marketplace_credentials():
            response = service.detail("45800")

        self.assertEqual(response.result.pricing.currency, "USD")
        self.assertEqual(requested_marketplace_ids, ["EBAY_US"])
        self.assertEqual(response.marketplaceListings[0].currency, "USD")

    def test_detail_uses_cached_ebay_listings_without_live_fetch(self) -> None:
        live_ebay_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if "catalog_marketplace_source_flags" in url:
                return httpx.Response(200, json=[{"source": "ebay", "enabled": True}])
            if "ebay_listing_cache" in url and request.method == "GET":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "listings": [
                                {
                                    "title": "Cached Listing",
                                    "price": 19.99,
                                    "currency": "AUD",
                                    "condition": "Used",
                                    "url": "https://www.ebay.com/itm/cached",
                                }
                            ]
                        }
                    ],
                )
            if "identity/v1/oauth2/token" in url or "buy/browse/v1/item_summary/search" in url:
                live_ebay_requests.append(request)
                return httpx.Response(200, json={})
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "45800",
                            "product_name": "God of War",
                            "console_name": "Playstation 4",
                            "category": "Action & Adventure",
                            "loose_price_cents": 1299,
                            "currency": "USD",
                            "normalized_identity": "god of war playstation 4",
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.detail("45800", currency="AUD")

        self.assertEqual(len(response.marketplaceListings), 1)
        self.assertEqual(response.marketplaceListings[0].title, "Cached Listing")
        self.assertEqual(live_ebay_requests, [])

    def test_detail_skips_marketplace_sources_when_all_disabled_via_flag(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if "catalog_marketplace_source_flags" in url:
                return httpx.Response(
                    200,
                    json=[
                        {"source": "ebay", "enabled": False},
                        {"source": "pricecharting", "enabled": False},
                    ],
                )
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "45800",
                            "product_name": "God of War",
                            "console_name": "Playstation 4",
                            "category": "Action & Adventure",
                            "loose_price_cents": 1299,
                            "currency": "USD",
                            "normalized_identity": "god of war playstation 4",
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.detail("45800", currency="AUD")

        self.assertEqual(response.marketplaceListings, [])

    def test_detail_skips_only_the_disabled_marketplace_source(self) -> None:
        # ebay disabled, pricecharting left enabled (fail-open default) --
        # confirms each source's kill switch is independent, not an
        # all-or-nothing flag.
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if "catalog_marketplace_source_flags" in url:
                return httpx.Response(200, json=[{"source": "ebay", "enabled": False}])
            if "pricecharting_listing_cache" in url and request.method == "GET":
                return httpx.Response(200, json=[])
            if "pricecharting_listing_cache" in url and request.method == "POST":
                return httpx.Response(201, json=None)
            if "/api/offers" in url:
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "offers": [
                            {
                                "product-name": "God of War",
                                "price": 1599,
                                "condition-string": "Normal wear",
                                "offer-url": "/offer/abc123",
                            },
                        ],
                    },
                )
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "45800",
                            "product_name": "God of War",
                            "console_name": "Playstation 4",
                            "category": "Action & Adventure",
                            "loose_price_cents": 1299,
                            "currency": "USD",
                            "normalized_identity": "god of war playstation 4",
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with _patched_marketplace_credentials():
            response = service.detail("45800")

        self.assertEqual(len(response.marketplaceListings), 1)
        self.assertEqual(response.marketplaceListings[0].source, "PriceCharting")

    def test_detail_merges_ebay_and_pricecharting_listings_with_currency_conversion(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if "catalog_marketplace_source_flags" in url:
                return httpx.Response(200, json=[])
            if "ebay_listing_cache" in url and request.method == "GET":
                return httpx.Response(200, json=[])
            if "ebay_listing_cache" in url and request.method == "POST":
                return httpx.Response(201, json=None)
            if "identity/v1/oauth2/token" in url:
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 7200})
            if "buy/browse/v1/item_summary/search" in url:
                return httpx.Response(
                    200,
                    json={
                        "itemSummaries": [
                            {
                                "title": "God of War PS4 Brand New",
                                "price": {"value": "20.00", "currency": "AUD"},
                                "condition": "New",
                                "itemWebUrl": "https://www.ebay.com.au/itm/12345",
                            },
                        ]
                    },
                )
            if "pricecharting_listing_cache" in url and request.method == "GET":
                return httpx.Response(200, json=[])
            if "pricecharting_listing_cache" in url and request.method == "POST":
                return httpx.Response(201, json=None)
            if "/api/offers" in url:
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "offers": [
                            {
                                "product-name": "God of War",
                                "price": 1000,  # $10.00 USD
                                "condition-string": "Normal wear",
                                "offer-url": "/offer/abc123",
                            },
                        ],
                    },
                )
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "45800",
                            "product_name": "God of War",
                            "console_name": "Playstation 4",
                            "category": "Action & Adventure",
                            "loose_price_cents": 1299,
                            "currency": "USD",
                            "normalized_identity": "god of war playstation 4",
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with _patched_marketplace_credentials():
            response = service.detail("45800", currency="AUD")

        self.assertEqual(len(response.marketplaceListings), 2)
        by_source = {listing.source: listing for listing in response.marketplaceListings}
        self.assertIn("eBay", by_source)
        self.assertIn("PriceCharting", by_source)
        self.assertEqual(by_source["eBay"].price, 20.00)
        self.assertEqual(by_source["eBay"].currency, "AUD")
        # $10.00 USD converted at the default rate (settings.fx_usd_to_aud,
        # 1.52) -- PriceCharting has no per-region marketplace, so this is
        # real currency conversion, not a different marketplace's native
        # price the way eBay's AUD figure above already was.
        self.assertEqual(by_source["PriceCharting"].price, round(10.00 * 1.52, 2))
        self.assertEqual(by_source["PriceCharting"].currency, "AUD")

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

    def test_search_renders_lorcana_thumbnail_inline(self) -> None:
        # Owner decision 2026-08-30: Lorcana search rows render the
        # publisher-CDN image inline (imageUrl) instead of exposing it
        # only as an externalImageUrl link.
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "lorcana_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "image_url": (
                                "https://api.lorcana.ravensburger.com/images/en/set13/70.jpg"
                            )
                        }
                    ],
                )
            if "search_pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "9001",
                            "product_name": "Elsa - Spirit of Winter #70",
                            "console_name": "Lorcana Archazia's Island",
                            "category": "Lorcana Archazia's Island",
                            "loose_price_cents": 4200,
                            "currency": "USD",
                            "normalized_identity": "elsa spirit of winter lorcana",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("elsa lorcana", limit=10)

        self.assertEqual(
            response.results[0].imageUrl,
            "https://api.lorcana.ravensburger.com/images/en/set13/70.jpg",
        )
        self.assertIsNone(response.results[0].externalImageUrl)

    def test_search_renders_magic_thumbnail_inline(self) -> None:
        # Owner decision 2026-08-30: Magic search rows render the
        # Scryfall image inline (imageUrl) instead of exposing it only
        # as an externalImageUrl link.
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "scryfall_magic_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "image_url": (
                                "https://cards.scryfall.io/normal/gilded-charm.jpg"
                            )
                        }
                    ],
                )
            if "search_pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "3773958",
                            "product_name": "Cabaretti Charm [Gilded Foil] #365",
                            "console_name": "Magic Streets of New Capenna",
                            "category": "Magic Streets of New Capenna",
                            "loose_price_cents": 5000,
                            "currency": "USD",
                            "normalized_identity": "cabaretti charm magic",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("cabaretti charm", limit=10)

        self.assertEqual(
            response.results[0].imageUrl,
            "https://cards.scryfall.io/normal/gilded-charm.jpg",
        )
        self.assertIsNone(response.results[0].externalImageUrl)

    def test_search_renders_lego_thumbnail_inline(self) -> None:
        # Owner decision 2026-08-30: LEGO search rows render the
        # Rebrickable image inline (imageUrl); the app shows a linked
        # Rebrickable attribution wherever this imagery appears.
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "name": "Titanic",
                            "image_url": (
                                "https://cdn.rebrickable.com/media/sets/10294-1.jpg"
                            ),
                        }
                    ],
                )
            if "search_pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "7700001",
                            "product_name": "Titanic #10294",
                            "console_name": "LEGO Sculptures",
                            "category": "LEGO Sculptures",
                            "loose_price_cents": 45000,
                            "currency": "USD",
                            "normalized_identity": "titanic lego sculptures",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("lego titanic", limit=10)

        self.assertEqual(
            response.results[0].imageUrl,
            "https://cdn.rebrickable.com/media/sets/10294-1.jpg",
        )
        self.assertIsNone(response.results[0].externalImageUrl)

    def test_search_renders_onepiece_thumbnail_inline(self) -> None:
        # Owner decision 2026-08-30: One Piece search rows render the
        # optcgapi image inline (imageUrl) instead of exposing it only
        # as an externalImageUrl link.
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "one_piece_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "card_name": "Monkey D Luffy",
                            "is_plain": True,
                            "image_url": (
                                "https://optcgapi.com/images/OP01-003.png"
                            ),
                        }
                    ],
                )
            if "search_pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "6600001",
                            "product_name": "Monkey D Luffy OP01-003",
                            "console_name": "One Piece Romance Dawn",
                            "category": "One Piece Romance Dawn",
                            "loose_price_cents": 900,
                            "currency": "USD",
                            "normalized_identity": "monkey d luffy one piece",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("monkey d luffy", limit=10)

        self.assertEqual(
            response.results[0].imageUrl,
            "https://optcgapi.com/images/OP01-003.png",
        )
        self.assertIsNone(response.results[0].externalImageUrl)

    def test_search_renders_yugioh_thumbnail_inline(self) -> None:
        # Owner decision 2026-08-30, contingent on re-hosting: Yu-Gi-Oh
        # search rows render the (self-hosted) image inline. The fixture
        # URL mirrors the catalog-images bucket rows produced by
        # scripts/rehost_yugioh_images.py.
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "yugioh_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "image_url": (
                                "https://example.supabase.co/storage/v1/object/public/catalog-images/yugioh/ygo/89631139.jpg"
                            )
                        }
                    ],
                )
            if "search_pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "5500001",
                            "product_name": "Blue-Eyes White Dragon LOB-001",
                            "console_name": "YuGiOh Legend of Blue Eyes White Dragon",
                            "category": "YuGiOh Legend of Blue Eyes White Dragon",
                            "loose_price_cents": 4500,
                            "currency": "USD",
                            "normalized_identity": "blue eyes white dragon yugioh",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("blue eyes white dragon", limit=10)

        self.assertEqual(
            response.results[0].imageUrl,
            "https://example.supabase.co/storage/v1/object/public/catalog-images/yugioh/ygo/89631139.jpg",
        )
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

    def _handler(
        self,
        *,
        search_row: dict,
        tcgplayer_rows: dict,
        sibling_rows: dict,
        tcgdex_rows: dict | None = None,
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "funko_pop_catalog" in url:
                return httpx.Response(200, json=[])
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if "tcgdex_pokemon_catalog" in url:
                params = request.url.params
                language = params.get("language", "").removeprefix("eq.")
                set_selector = (
                    params.get("set_key", "").removeprefix("eq.")
                    or params.get("set_name", "").removeprefix("eq.")
                )
                number = params.get("local_id_norm", "").removeprefix("eq.")
                return httpx.Response(
                    200,
                    json=(tcgdex_rows or {}).get(
                        (language, set_selector, number), []
                    ),
                )
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

    def test_search_attaches_batched_tcgdex_thumbnails(self) -> None:
        # Search-surface Pokemon thumbnails: one batched tcgdex query per
        # language for the whole page (never per-row), low.webp attached
        # inline for plain rows, bracket-variant rows left to the
        # link-only chain, Japanese via the hand map.
        tcgdex_requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_kicksdb_catalog" in url:
                return httpx.Response(200, json=[])
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "tcgdex_pokemon_catalog" in url:
                tcgdex_requests.append(url)
                language = request.url.params.get("language", "")
                if language == "eq.en":
                    return httpx.Response(200, json=[{
                        "set_key": "stellar crown",
                        "set_name": "Stellar Crown",
                        "local_id_norm": "25",
                        "image_url": "https://assets.tcgdex.net/en/sv/sv07/025",
                    }])
                return httpx.Response(200, json=[{
                    "set_key": "クレイバースト",
                    "set_name": "クレイバースト",
                    "local_id_norm": "27",
                    "image_url": "https://assets.tcgdex.net/ja/SV/SV2D/027",
                }])
            if "search_pricecharting_catalog" in url:
                return httpx.Response(200, json=[
                    {
                        "pricecharting_id": "1",
                        "product_name": "Pikachu #25",
                        "console_name": "Pokemon Stellar Crown",
                        "category": "Pokemon Card",
                        "loose_price_cents": 500,
                        "currency": "USD",
                        "source_file": "pokemon.csv",
                    },
                    {
                        "pricecharting_id": "2",
                        "product_name": "Pikachu [Reverse Holo] #25",
                        "console_name": "Pokemon Stellar Crown",
                        "category": "Pokemon Card",
                        "loose_price_cents": 900,
                        "currency": "USD",
                        "source_file": "pokemon.csv",
                    },
                    {
                        "pricecharting_id": "3",
                        "product_name": "Wigglytuff #27",
                        "console_name": "Pokemon Japanese Clay Burst",
                        "category": "Pokemon Card",
                        "loose_price_cents": 300,
                        "currency": "USD",
                        "source_file": "pokemon.csv",
                    },
                ])
            return httpx.Response(200, json=[])

        service = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        response = service.search("pikachu", limit=10)

        by_id = {r.id: r for r in response.results}
        self.assertEqual(
            by_id["1"].imageUrl,
            "https://assets.tcgdex.net/en/sv/sv07/025/low.webp",
        )
        # The row's external link must open the full-size asset, not the
        # 245px thumbnail.
        self.assertEqual(
            by_id["1"].externalImageUrl,
            "https://assets.tcgdex.net/en/sv/sv07/025/high.webp",
        )
        self.assertEqual(
            by_id["3"].imageUrl,
            "https://assets.tcgdex.net/ja/SV/SV2D/027/low.webp",
        )
        # Variant row: no inline thumbnail (print ambiguity), link-only.
        self.assertIsNone(by_id["2"].imageUrl)
        batched = [u for u in tcgdex_requests if "or=" in u]
        self.assertEqual(len(batched), 2)  # one per language, not per row

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

    def _service(self, **handler_kwargs) -> CatalogSearchService:
        return CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(
                transport=httpx.MockTransport(self._handler(**handler_kwargs))
            ),
        )

    def test_detail_uses_tcgdex_image_for_modern_set(self) -> None:
        # The five-set TCGplayer dict never covered modern sets; TCGdex
        # covers every English expansion by normalized set-name equality.
        service = self._service(
            search_row={
                "pricecharting_id": "990001",
                "product_name": "Venusaur ex #1",
                "console_name": "Pokemon Stellar Crown",
                "category": "Pokemon Card",
                "loose_price_cents": 900,
                "currency": "USD",
            },
            tcgplayer_rows={},
            sibling_rows={
                "Pokemon Stellar Crown": [{"pricecharting_id": "990001"}]
            },
            tcgdex_rows={
                ("en", "stellar crown", "1"): [
                    {"image_url": "https://assets.tcgdex.net/en/sv/sv07/001"}
                ],
            },
        )

        response = service.detail("990001")

        self.assertEqual(
            response.result.imageUrl,
            "https://assets.tcgdex.net/en/sv/sv07/001/high.webp",
        )

    def test_detail_tcgdex_miss_falls_back_to_tcgplayer_classic_set(self) -> None:
        service = self._service(
            search_row={
                "pricecharting_id": "630417",
                "product_name": "Charizard #4",
                "console_name": "Pokemon Base Set",
                "category": "Pokemon Card",
                "loose_price_cents": 16100,
                "currency": "USD",
            },
            tcgplayer_rows={
                ("Base Set", "4"): [
                    {
                        "product_name": "Charizard",
                        "image_url": "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
                        "variant_tag": None,
                    },
                ],
            },
            sibling_rows={"Pokemon Base Set": [{"pricecharting_id": "630417"}]},
            tcgdex_rows={},
        )

        response = service.detail("630417")

        self.assertEqual(
            response.result.imageUrl,
            "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
        )

    def test_detail_plain_row_gets_tcgdex_image_despite_variant_siblings(self) -> None:
        # TCGdex images are canonical base-print scans, so the PLAIN row is
        # exactly what the photo depicts -- bracket-tagged siblings must
        # not suppress it. Real bug found live on rollout day: search
        # showed a thumbnail for a plain row (Area Zero Underdepths #131,
        # which has [Reverse Holo]/[Prize Pack]/[Gym Stamp Asia] siblings)
        # while its detail page suppressed the same image.
        service = self._service(
            search_row={
                "pricecharting_id": "990002",
                "product_name": "Pikachu #25",
                "console_name": "Pokemon Stellar Crown",
                "category": "Pokemon Card",
                "loose_price_cents": 500,
                "currency": "USD",
            },
            tcgplayer_rows={},
            sibling_rows={
                "Pokemon Stellar Crown": [
                    {"pricecharting_id": "990002", "product_name": "Pikachu #25"},
                    {
                        "pricecharting_id": "990003",
                        "product_name": "Pikachu [Reverse Holo] #25",
                    },
                ]
            },
            tcgdex_rows={
                ("en", "stellar crown", "25"): [
                    {"image_url": "https://assets.tcgdex.net/en/sv/sv07/025"}
                ],
            },
        )

        response = service.detail("990002")

        self.assertEqual(
            response.result.imageUrl,
            "https://assets.tcgdex.net/en/sv/sv07/025/high.webp",
        )

    def test_detail_bracketed_variant_row_never_gets_tcgdex_image(self) -> None:
        # An unstamped base scan on a [Reverse Holo] row would be visibly
        # wrong -- bracketed rows only ever get the TCGplayer variant-exact
        # image, never the TCGdex base scan.
        service = self._service(
            search_row={
                "pricecharting_id": "990003",
                "product_name": "Pikachu [Reverse Holo] #25",
                "console_name": "Pokemon Stellar Crown",
                "category": "Pokemon Card",
                "loose_price_cents": 900,
                "currency": "USD",
            },
            tcgplayer_rows={},
            sibling_rows={
                "Pokemon Stellar Crown": [
                    {"pricecharting_id": "990003",
                     "product_name": "Pikachu [Reverse Holo] #25"},
                ]
            },
            tcgdex_rows={
                ("en", "stellar crown", "25"): [
                    {"image_url": "https://assets.tcgdex.net/en/sv/sv07/025"}
                ],
            },
        )

        response = service.detail("990003")

        self.assertIsNone(response.result.imageUrl)

    def test_detail_japanese_mapped_set_uses_tcgdex_ja(self) -> None:
        service = self._service(
            search_row={
                "pricecharting_id": "990004",
                "product_name": "Superior Energy Retrieval #98",
                "console_name": "Pokemon Japanese Clay Burst",
                "category": "Pokemon Card",
                "loose_price_cents": 300,
                "currency": "USD",
            },
            tcgplayer_rows={},
            sibling_rows={
                "Pokemon Japanese Clay Burst": [{"pricecharting_id": "990004"}]
            },
            tcgdex_rows={
                ("ja", "クレイバースト", "98"): [
                    {"image_url": "https://assets.tcgdex.net/ja/sv/sv2D/098"}
                ],
            },
        )

        response = service.detail("990004")

        self.assertEqual(
            response.result.imageUrl,
            "https://assets.tcgdex.net/ja/sv/sv2D/098/high.webp",
        )

    def test_detail_japanese_unmapped_set_gets_no_image(self) -> None:
        # Unmapped Japanese sets mean no attempt at all -- deterministic
        # mapping only, never a fuzzy cross-language guess.
        service = self._service(
            search_row={
                "pricecharting_id": "990005",
                "product_name": "Meganium #154",
                "console_name": "Pokemon Japanese Gold, Silver, New World",
                "category": "Pokemon Card",
                "loose_price_cents": 4000,
                "currency": "USD",
            },
            tcgplayer_rows={},
            sibling_rows={},
            tcgdex_rows={},
        )

        response = service.detail("990005")

        self.assertIsNone(response.result.imageUrl)

    def test_detail_promo_number_routes_to_black_star_promos(self) -> None:
        service = self._service(
            search_row={
                "pricecharting_id": "990006",
                "product_name": "Pikachu #SM210",
                "console_name": "Pokemon Promo",
                "category": "Pokemon Card",
                "loose_price_cents": 1200,
                "currency": "USD",
            },
            tcgplayer_rows={},
            sibling_rows={"Pokemon Promo": [{"pricecharting_id": "990006"}]},
            tcgdex_rows={
                ("en", "sm black star promos", "sm210"): [
                    {"image_url": "https://assets.tcgdex.net/en/smp/SM210"}
                ],
            },
        )

        response = service.detail("990006")

        self.assertEqual(
            response.result.imageUrl,
            "https://assets.tcgdex.net/en/smp/SM210/high.webp",
        )

    def test_detail_tcgdex_ambiguous_rows_are_not_trusted(self) -> None:
        service = self._service(
            search_row={
                "pricecharting_id": "990007",
                "product_name": "Eevee #75",
                "console_name": "Pokemon Stellar Crown",
                "category": "Pokemon Card",
                "loose_price_cents": 200,
                "currency": "USD",
            },
            tcgplayer_rows={},
            sibling_rows={
                "Pokemon Stellar Crown": [{"pricecharting_id": "990007"}]
            },
            tcgdex_rows={
                ("en", "stellar crown", "75"): [
                    {"image_url": "https://assets.tcgdex.net/en/sv/sv07/075"},
                    {"image_url": "https://assets.tcgdex.net/en/sv/sv07/075b"},
                ],
            },
        )

        response = service.detail("990007")

        self.assertIsNone(response.result.imageUrl)

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
                {"pricecharting_id": "630417", "product_name": "Charizard #4"},
                {
                    "pricecharting_id": "7096109",
                    "product_name": "Charizard [1999-2000] #4",
                },
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

    def test_detail_resolves_vintage_set_alias_by_name(self) -> None:
        # PriceCharting's "Magic Beta" is Scryfall's "Limited Edition
        # Beta" -- without the alias table the game's most iconic cards
        # (live-verified: Black Lotus) resolved to no set and no image.
        search_row = {
            "pricecharting_id": "8800001",
            "product_name": "Black Lotus",
            "console_name": "Magic Beta",
            "category": "Magic Beta",
            "loose_price_cents": 2500000,
            "currency": "USD",
        }
        name_rows = {
            ("limited edition beta", "black lotus"): [
                {"image_url": "https://cards.scryfall.io/normal/beta-black-lotus.jpg"},
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

        response = service.detail("8800001")

        self.assertEqual(
            response.result.imageUrl,
            "https://cards.scryfall.io/normal/beta-black-lotus.jpg",
        )

    def test_detail_resolves_possessive_set_alias_by_number(self) -> None:
        # PriceCharting's "Magic Marvel Spider-Man" is Scryfall's
        # "Marvel's Spider-Man" (the 's normalizes to "marvels"), which
        # missed until the alias -- live-caught: The Soul Stone #242.
        search_row = {
            "pricecharting_id": "8800003",
            "product_name": "The Soul Stone #242",
            "console_name": "Magic Marvel Spider-Man",
            "category": "Magic Marvel Spider-Man",
            "loose_price_cents": 2077828,
            "currency": "USD",
        }
        number_rows = {
            ("marvels spider man", "242"): [
                {"image_url": "https://cards.scryfall.io/normal/soul-stone.jpg"},
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

        response = service.detail("8800003")

        self.assertEqual(
            response.result.imageUrl,
            "https://cards.scryfall.io/normal/soul-stone.jpg",
        )

    def test_detail_resolves_renamed_commander_set_alias_by_number(self) -> None:
        # "Magic Lord of the Rings Commander" is Scryfall's "Tales of
        # Middle-earth Commander" -- a numbered modern card that still
        # missed because the set name never resolved.
        search_row = {
            "pricecharting_id": "8800002",
            "product_name": "Dwarven Sol Ring #409",
            "console_name": "Magic Lord of the Rings Commander",
            "category": "Magic Lord of the Rings Commander",
            "loose_price_cents": 1500,
            "currency": "USD",
        }
        number_rows = {
            ("tales of middle earth commander", "409"): [
                {"image_url": "https://cards.scryfall.io/normal/dwarven-sol-ring.jpg"},
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

        response = service.detail("8800002")

        self.assertEqual(
            response.result.imageUrl,
            "https://cards.scryfall.io/normal/dwarven-sol-ring.jpg",
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


class CatalogSearchVideoGameEnrichmentTest(unittest.TestCase):
    def _build_handler(self, *, catalog_rows: list[dict] | None = None):
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "555",
                            "product_name": "Super Mario 64",
                            "console_name": "Nintendo 64",
                            "category": "Nintendo 64",
                            "loose_price_cents": 3000,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                return httpx.Response(200, json=catalog_rows or [])
            return httpx.Response(200, json=[])

        return handler, catalog_requests

    def _build_service(self, handler) -> CatalogSearchService:
        return CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_detail_enriches_video_game_with_confirmed_single_match(self) -> None:
        handler, catalog_requests = self._build_handler(
            catalog_rows=[{"image_url": "https://img.example/mario64.jpg"}]
        )
        service = self._build_service(handler)

        response = service.detail("555")

        self.assertEqual(response.result.imageUrl, "https://img.example/mario64.jpg")
        self.assertEqual(len(catalog_requests), 1)
        query = dict(catalog_requests[0].url.params)
        self.assertEqual(query.get("normalized_name"), "eq.super mario 64")
        self.assertEqual(query.get("rawg_platform"), "eq.Nintendo 64")

    def test_video_game_strip_edition_suffix_strips_remaster_and_edition_words(
        self,
    ) -> None:
        # A live, systematic audit found 1,083 real PriceCharting video-
        # game titles ("Remastered"/"HD"/"Definitive Edition"/etc.) with no
        # match through any existing tier -- and confirmed via RAWG's live
        # search API that most of these games DO exist in RAWG, just
        # under the base title with no separate remaster-specific entry.
        self.assertEqual(
            _video_game_strip_edition_suffix("valkyria chronicles remastered"),
            "valkyria chronicles",
        )
        self.assertEqual(
            _video_game_strip_edition_suffix("dying light: definitive edition"),
            "dying light",
        )
        self.assertEqual(
            _video_game_strip_edition_suffix("beholder complete edition"),
            "beholder",
        )
        self.assertEqual(
            _video_game_strip_edition_suffix("gran turismo 6 anniversary edition"),
            "gran turismo 6",
        )
        # No suffix present -- must return None (not the unchanged
        # string), so the caller knows not to bother retrying.
        self.assertIsNone(_video_game_strip_edition_suffix("mario kart 8"))

    def test_video_game_strip_edition_suffix_does_not_strip_remake(self) -> None:
        # The one deliberate exclusion: a remake is a distinct, separately
        # -developed product with its own real box art (Final Fantasy VII
        # Remake, Resident Evil 4 Remake), not a technical re-release
        # reusing the original's key art -- live-confirmed that stripping
        # "remake" would have shown the ORIGINAL 1997 Final Fantasy VII's
        # cover on a "Final Fantasy VII Remake" listing.
        self.assertIsNone(
            _video_game_strip_edition_suffix("final fantasy vii remake")
        )
        self.assertIsNone(
            _video_game_strip_edition_suffix("resident evil 4 remake")
        )

    def test_video_game_strip_punctuation_treats_hyphen_as_space(self) -> None:
        # Real gap found while auditing: RAWG's "E.T. the Extra-Terrestrial"
        # vs PriceCharting's "ET the Extra Terrestrial" (a space, no
        # hyphen) only differ by that one character, but deleting the
        # hyphen with no replacement joined the two words into
        # "extraterrestrial" -- one word short of "extra terrestrial",
        # so they never compared equal even though it's the same game.
        self.assertEqual(
            _video_game_strip_punctuation("et the extra-terrestrial"),
            "et the extra terrestrial",
        )
        self.assertEqual(
            _video_game_strip_punctuation("et the extra terrestrial"),
            "et the extra terrestrial",
        )

    def test_detail_falls_back_to_the_prefix_when_pricecharting_drops_the_article(
        self,
    ) -> None:
        # Real, live-confirmed gap: PriceCharting frequently drops a
        # leading "The" that RAWG's own title keeps -- e.g. PriceCharting's
        # "Witcher 3: Wild Hunt" vs RAWG's "The Witcher 3: Wild Hunt",
        # "Elder Scrolls V: Skyrim" vs "The Elder Scrolls V: Skyrim".
        # Confirmed live against real data for two major franchises.
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "888",
                            "product_name": "Witcher 3 Wild Hunt",
                            "console_name": "Playstation 4",
                            "category": "RPG",
                            "loose_price_cents": 2000,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                query = dict(request.url.params)
                if query.get("normalized_name") == "eq.the witcher 3 wild hunt":
                    return httpx.Response(
                        200,
                        json=[{"image_url": "https://img.example/witcher3.jpg"}],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.detail("888")

        self.assertEqual(response.result.imageUrl, "https://img.example/witcher3.jpg")

    def test_detail_the_prefix_fallback_never_strips_an_existing_the(self) -> None:
        # A title genuinely already starting with "the" must not get a
        # second "the " prepended -- confirms the fallback only ever
        # ADDS the article, never guesses at removing or duplicating one.
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "889",
                            "product_name": "The Nonexistent Game",
                            "console_name": "Playstation 4",
                            "category": "RPG",
                            "loose_price_cents": 2000,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                query = dict(request.url.params)
                self.assertNotIn(
                    "the the nonexistent game", query.get("normalized_name", "")
                )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)
        response = service.detail("889")

        self.assertIsNone(response.result.imageUrl)

    def test_detail_falls_back_to_stripped_edition_suffix(self) -> None:
        # Integration-level proof: no match for the full title, but
        # stripping "remastered" and retrying the whole exact/prefix/
        # loose chain against the stripped title finds the base game.
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "777",
                            "product_name": "Valkyria Chronicles Remastered",
                            "console_name": "Nintendo Switch",
                            "category": "Strategy",
                            "loose_price_cents": 4000,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                query = dict(request.url.params)
                if query.get("normalized_name") == "eq.valkyria chronicles":
                    return httpx.Response(
                        200,
                        json=[{"image_url": "https://img.example/valkyria.jpg"}],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.detail("777")

        self.assertEqual(response.result.imageUrl, "https://img.example/valkyria.jpg")

    def test_video_game_prefix_suffix_rejects_numbered_sequels(self) -> None:
        # A live, systematic audit of every real PriceCharting video-game
        # title against rawg_video_game_catalog found 95 real cases where
        # the prefix fallback's ONLY unique match was a numbered sequel,
        # not a longer title for the same game -- e.g. PriceCharting's
        # "Terminator" uniquely prefix-matching RAWG's "Terminator 2:
        # Judgment Day". These must be rejected here.
        self.assertFalse(
            _video_game_prefix_suffix_is_safe(
                "terminator", "terminator 2: judgment day"
            )
        )
        self.assertFalse(
            _video_game_prefix_suffix_is_safe("pony friends", "pony friends 2")
        )
        self.assertFalse(_video_game_prefix_suffix_is_safe("iron man", "iron man 2"))
        self.assertFalse(
            _video_game_prefix_suffix_is_safe(
                "zelda", "zelda ii: the adventure of link"
            )
        )
        self.assertFalse(
            _video_game_prefix_suffix_is_safe("contra", "contra iii: the alien wars")
        )

    def test_video_game_prefix_suffix_accepts_genuine_longer_titles(self) -> None:
        # The far more common, legitimate case this fallback exists for --
        # RAWG's own title is simply longer (an edition/release-year/
        # official-subtitle suffix), same game, must still be accepted.
        self.assertTrue(
            _video_game_prefix_suffix_is_safe(
                "rio", "rio: the multiplayer party game"
            )
        )
        self.assertTrue(_video_game_prefix_suffix_is_safe("grid", "grid (2008)"))
        self.assertTrue(
            _video_game_prefix_suffix_is_safe("battleship", "battleship (1993)")
        )
        self.assertTrue(
            _video_game_prefix_suffix_is_safe(
                "hitman 2", "hitman 2: silent assassin"
            )
        )
        self.assertTrue(
            _video_game_prefix_suffix_is_safe(
                "brothers", "brothers a tale of two sons"
            )
        )

    def test_detail_rejects_prefix_match_that_is_actually_a_sequel(self) -> None:
        # Integration-level proof for the same bug class as the two pure-
        # function tests above: the prefix tier finds a unique candidate,
        # but it's the wrong game (a sequel) -- must fall through to the
        # loose-match tier (which also correctly finds nothing here) rather
        # than returning the sequel's cover for the original's listing.
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "999",
                            "product_name": "Terminator",
                            "console_name": "PAL NES",
                            "category": "Action",
                            "loose_price_cents": 500,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                query = dict(request.url.params)
                normalized_name_param = query.get("normalized_name", "")
                if normalized_name_param == "like.terminator*":
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "normalized_name": "terminator 2: judgment day",
                                "image_url": "https://img.example/terminator2.jpg",
                            },
                        ],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.detail("999")

        self.assertIsNone(response.result.imageUrl)

    def test_video_game_strip_punctuation_folds_accented_letters_not_deletes_them(
        self,
    ) -> None:
        # The bug this generalizes a fix for: without folding, an accented
        # letter is simply removed by the non-alphanumeric strip (it isn't
        # in [a-z0-9 ]) rather than reduced to its plain ASCII base letter
        # -- "ragnarök" would become "ragnark" (one letter short, a
        # different string), not "ragnarok". Covers a few distinct
        # diacritic types (umlaut, acute accent), not just the one
        # live-confirmed case.
        # Always called on already-lowercased normalized_name values in
        # real usage (both sides come from _video_game_normalize_name() or
        # the DB column, which the import script lowercases at write
        # time) -- exercised lowercase here to match, since the strip
        # regex is deliberately case-sensitive ([a-z0-9 ], not
        # [a-zA-Z0-9 ]) and would otherwise strip capital letters as if
        # they were punctuation, an unrelated behavior this test isn't
        # about.
        self.assertEqual(_video_game_strip_punctuation("ragnarök"), "ragnarok")
        self.assertEqual(_video_game_strip_punctuation("pokémon"), "pokemon")
        self.assertEqual(_video_game_strip_punctuation("café racer"), "cafe racer")
        # Still folds curly-vs-straight apostrophes and strips ordinary
        # punctuation the same as before this change.
        self.assertEqual(
            _video_game_strip_punctuation("uncharted 4: a thief’s end"),
            "uncharted 4 a thiefs end",
        )

    def test_detail_resolves_god_of_war_ragnarok_alias_to_rawgs_real_title(
        self,
    ) -> None:
        # RAWG's actual title is "God of War: Ragnarök" (colon + the
        # Old Norse ö) -- PriceCharting's listing is plain "God of War
        # Ragnarok". The alias must send the EXACT ("god of war: ragnarök")
        # query on the first (exact-match) tier -- if this alias were
        # missing, live testing showed the loose-match fallback tier
        # picks an unrelated "Ragnarok: Valhalla" DLC screenshot instead
        # of the real game's cover, a worse outcome than no image at all.
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "4557968",
                            "product_name": "God of War Ragnarok",
                            "console_name": "Playstation 5",
                            "category": "Action & Adventure",
                            "loose_price_cents": 2285,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                query = dict(request.url.params)
                if query.get("normalized_name") == "eq.god of war: ragnarök":
                    return httpx.Response(
                        200,
                        json=[{"image_url": "https://img.example/gow-ragnarok.jpg"}],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.detail("4557968")

        self.assertEqual(response.result.imageUrl, "https://img.example/gow-ragnarok.jpg")
        self.assertEqual(len(catalog_requests), 1)

    def test_search_batches_video_game_image_lookup_in_one_request(self) -> None:
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "555",
                            "product_name": "Super Mario 64",
                            "console_name": "Nintendo 64",
                            "category": "Nintendo 64",
                            "loose_price_cents": 3000,
                            "currency": "USD",
                        },
                        {
                            "pricecharting_id": "556",
                            "product_name": "Worms",
                            "console_name": "Gameboy",
                            "category": "Game Boy",
                            "loose_price_cents": 500,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                query = dict(request.url.params)
                if query.get("rawg_platform") == "eq.Nintendo 64":
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "normalized_name": "super mario 64",
                                "image_url": "https://img.example/mario64.jpg",
                            },
                        ],
                    )
                if query.get("rawg_platform") == "eq.Game Boy":
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "normalized_name": "worms",
                                "image_url": "https://img.example/worms.jpg",
                            },
                        ],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.search("mario worms", limit=20)

        # One request per distinct PLATFORM (Nintendo 64, Game Boy),
        # regardless of result count -- amortizes the full matching chain
        # across however many rows share a platform, rather than one
        # request per row or per exact title.
        self.assertEqual(len(catalog_requests), 2)
        by_id = {result.id: result for result in response.results}
        self.assertEqual(by_id["555"].imageUrl, "https://img.example/mario64.jpg")
        self.assertEqual(by_id["556"].imageUrl, "https://img.example/worms.jpg")

    def test_search_suppresses_ambiguous_video_game_pair_in_batch(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "555",
                            "product_name": "Super Mario 64",
                            "console_name": "Nintendo 64",
                            "category": "Nintendo 64",
                            "loose_price_cents": 3000,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                # Two rows share the same (normalized_name, rawg_platform)
                # pair -- ambiguous, must be suppressed rather than
                # guessing, same discipline as the single-item path.
                return httpx.Response(
                    200,
                    json=[
                        {
                            "normalized_name": "super mario 64",
                            "rawg_platform": "Nintendo 64",
                            "image_url": "https://img.example/mario64-a.jpg",
                        },
                        {
                            "normalized_name": "super mario 64",
                            "rawg_platform": "Nintendo 64",
                            "image_url": "https://img.example/mario64-b.jpg",
                        },
                    ],
                )
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.search("mario", limit=20)

        self.assertIsNone(response.results[0].imageUrl)

    def test_search_row_resolves_via_the_prefix_fallback_not_just_exact(
        self,
    ) -> None:
        # The actual reported bug this upgrade fixes: a title that only
        # resolves via the "the "-prefix + loose-match fallback (e.g.
        # PriceCharting's "Witcher 3 Wild Hunt" vs RAWG's "The Witcher 3:
        # Wild Hunt") used to stay a placeholder on the search results row
        # even though detail() could already resolve it -- the row-level
        # batch only ever tried an exact match. Now the row gets the same
        # answer detail() would give, without the user needing to tap in.
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "555",
                            "product_name": "Witcher 3 Wild Hunt",
                            "console_name": "Playstation 4",
                            "category": "RPG",
                            "loose_price_cents": 2000,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                query = dict(request.url.params)
                if query.get("rawg_platform") == "eq.PlayStation 4":
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "normalized_name": "the witcher 3: wild hunt",
                                "image_url": "https://img.example/witcher3.jpg",
                            },
                        ],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.search("witcher 3", limit=20)

        self.assertEqual(
            response.results[0].imageUrl, "https://img.example/witcher3.jpg"
        )

    def test_search_skips_video_game_enrichment_when_category_disabled(self) -> None:
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search_pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "555",
                            "product_name": "Super Mario 64",
                            "console_name": "Nintendo 64",
                            "category": "Nintendo 64",
                            "loose_price_cents": 3000,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(
                    200, json=[{"category": "videogames", "enabled": False}]
                )
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.search("mario", limit=20)

        self.assertEqual(catalog_requests, [])
        self.assertIsNone(response.results[0].imageUrl)

    def test_detail_suppresses_when_no_match(self) -> None:
        handler, catalog_requests = self._build_handler(catalog_rows=[])
        service = self._build_service(handler)

        response = service.detail("555")

        self.assertIsNone(response.result.imageUrl)
        # Exact match (0 rows) falls through to prefix, then loose-match,
        # then the edition-suffix fallback (no suffix here, so it's a
        # no-op) and the "the "-prefix fallback (a fresh exact/prefix/
        # loose chain against "the super mario 64") -- all also 0 rows
        # since the mock handler serves the same empty fixture regardless
        # of query, so still correctly suppressed, just via 6 requests now.
        self.assertEqual(len(catalog_requests), 6)

    def test_detail_suppresses_ambiguous_matches(self) -> None:
        handler, catalog_requests = self._build_handler(
            catalog_rows=[
                {"image_url": "https://img.example/mario64-a.jpg"},
                {"image_url": "https://img.example/mario64-b.jpg"},
            ]
        )
        service = self._build_service(handler)

        response = service.detail("555")

        self.assertIsNone(response.result.imageUrl)
        # Exact match (2 ambiguous rows) falls through to prefix, then
        # loose-match, then the "the "-prefix retry's own exact/prefix/
        # loose chain -- all of which the mock handler also serves the
        # same 2 rows for -- still ambiguous, still correctly suppressed,
        # just via 6 requests now (edition-suffix fallback is a no-op,
        # no suffix in this title).
        self.assertEqual(len(catalog_requests), 6)

    def test_detail_enriches_via_prefix_fallback_when_rawg_title_is_longer(self) -> None:
        # "Brothers" (PriceCharting) -> "Brothers: A Tale of Two Sons"
        # (RAWG) -- the far more common real-world gap than a franchise
        # reboot sharing an identical title: RAWG's title is simply longer.
        # The exact eq. request finds nothing; the like. prefix request
        # finds exactly one row and is used.
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "557",
                            "product_name": "Brothers",
                            "console_name": "Playstation 4",
                            "category": "Adventure",
                            "loose_price_cents": 1500,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                query = dict(request.url.params)
                if query.get("normalized_name", "").startswith("like."):
                    return httpx.Response(
                        200,
                        json=[{"image_url": "https://img.example/brothers.jpg"}],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.detail("557")

        self.assertEqual(response.result.imageUrl, "https://img.example/brothers.jpg")
        self.assertEqual(len(catalog_requests), 2)
        exact_query = dict(catalog_requests[0].url.params)
        self.assertEqual(exact_query.get("normalized_name"), "eq.brothers")
        prefix_query = dict(catalog_requests[1].url.params)
        self.assertEqual(prefix_query.get("normalized_name"), "like.brothers*")
        self.assertEqual(prefix_query.get("rawg_platform"), "eq.PlayStation 4")

    def test_detail_prefix_fallback_suppresses_multiple_candidates(self) -> None:
        # "Doom" prefix-matches DOOM (2016), DOOM Eternal, Doom 3, etc. --
        # genuinely different games, not just a longer spelling of the same
        # one, so this must stay suppressed rather than guess.
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "558",
                            "product_name": "Doom",
                            "console_name": "Playstation 4",
                            "category": "FPS",
                            "loose_price_cents": 1200,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                query = dict(request.url.params)
                normalized_name_param = query.get("normalized_name", "")
                if normalized_name_param.startswith(
                    "like."
                ) or normalized_name_param.startswith("ilike."):
                    return httpx.Response(
                        200,
                        json=[
                            {"image_url": "https://img.example/doom-2016.jpg"},
                            {"image_url": "https://img.example/doom-eternal.jpg"},
                        ],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.detail("558")

        self.assertIsNone(response.result.imageUrl)
        # Exact/prefix/loose all stay ambiguous (2 rows) for both "doom"
        # and the "the "-prefix retry's "the doom" -- 6 requests total.
        self.assertEqual(len(catalog_requests), 6)

    def test_detail_enriches_via_loose_match_for_mid_title_punctuation(self) -> None:
        # Real, live-confirmed case: PriceCharting's "Uncharted 4 A Thief's
        # End" (straight apostrophe, no colon) vs RAWG's "Uncharted 4: A
        # Thief's End" (curly apostrophe, colon after the number). The
        # colon breaks a leading-prefix match immediately after
        # "Uncharted 4" regardless of the apostrophe difference, so this
        # needs the loose-match tier specifically (both tiers before it
        # correctly find nothing).
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "559",
                            "product_name": "Uncharted 4 A Thief's End",
                            "console_name": "Playstation 4",
                            "category": "Action & Adventure",
                            "loose_price_cents": 1300,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                query = dict(request.url.params)
                normalized_name_param = query.get("normalized_name", "")
                if normalized_name_param.startswith("like.uncharted 4"):
                    # 3rd tier's filter-word contains pattern
                    # ("ilike.*uncharted*") is a superset match of the 2nd
                    # tier's full-title prefix ("like.uncharted 4 a
                    # thiefs end*") -- distinguish by checking for the
                    # full phrase vs. just the bare word.
                    return httpx.Response(200, json=[])
                if normalized_name_param == "ilike.*uncharted*":
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "normalized_name": "uncharted 4: a thief’s end",
                                "image_url": "https://img.example/uncharted4.jpg",
                            },
                        ],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.detail("559")

        self.assertEqual(response.result.imageUrl, "https://img.example/uncharted4.jpg")
        self.assertEqual(len(catalog_requests), 3)
        loose_query = dict(catalog_requests[2].url.params)
        self.assertEqual(loose_query.get("normalized_name"), "ilike.*uncharted*")

    def test_detail_skips_unmapped_video_game_platform(self) -> None:
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "556",
                            "product_name": "Some Obscure Game",
                            "console_name": "MSX2",
                            "category": "MSX2",
                            "loose_price_cents": 1000,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.detail("556")

        self.assertIsNone(response.result.imageUrl)
        self.assertEqual(len(catalog_requests), 0)

    def test_detail_strips_bracket_tag_before_lookup(self) -> None:
        catalog_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "pricecharting_catalog_history" in url:
                return httpx.Response(200, json=[])
            if request.method == "GET" and "/rest/v1/pricecharting_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "pricecharting_id": "557",
                            "product_name": "Super Mario 64 [Collector's Edition]",
                            "console_name": "Nintendo 64",
                            "category": "Nintendo 64",
                            "loose_price_cents": 3000,
                            "currency": "USD",
                        },
                    ],
                )
            if "catalog_image_source_flags" in url:
                return httpx.Response(200, json=[])
            if "rawg_video_game_catalog" in url:
                catalog_requests.append(request)
                return httpx.Response(
                    200, json=[{"image_url": "https://img.example/mario64.jpg"}]
                )
            return httpx.Response(200, json=[])

        service = self._build_service(handler)

        response = service.detail("557")

        self.assertEqual(response.result.imageUrl, "https://img.example/mario64.jpg")
        self.assertEqual(len(catalog_requests), 1)
        query = dict(catalog_requests[0].url.params)
        self.assertEqual(query.get("normalized_name"), "eq.super mario 64")


if __name__ == "__main__":
    unittest.main()
