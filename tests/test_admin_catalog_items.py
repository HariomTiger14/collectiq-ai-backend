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

    def test_service_batches_funko_image_lookup_for_pricecharting_rows(self) -> None:
        # PriceCharting has real pricing for Funko rows but no image data at
        # all -- the admin browse table enriches with a single batched
        # lookup against funko_pop_catalog, not one request per row.
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            url = str(request.url)
            if "funko_pop_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "normalized_title": "1950 batmobile",
                            "image_url": "https://images.hobbydb.com/1950-batmobile.png",
                            "series": ["Funko Vinyl Art Toys"],
                        },
                    ],
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "pricecharting_id": "7531531",
                        "product_name": "1950 Batmobile #277",
                        "category": "Batman: 80th Anniversary",
                        "console_name": "Funko POP Rides",
                        "loose_price_cents": 999,
                        "currency": "USD",
                        "updated_at": "2026-08-16T00:00:00Z",
                    },
                    {
                        "pricecharting_id": "999",
                        "product_name": "Charizard Base Set",
                        "category": "Trading Card",
                        "console_name": "Base Set",
                        "loose_price_cents": 38800,
                        "currency": "USD",
                        "updated_at": "2026-08-13T00:00:00Z",
                    },
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

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(
            items_by_id["7531531"]["imageUrl"],
            "https://images.hobbydb.com/1950-batmobile.png",
        )
        self.assertIsNone(items_by_id["999"]["imageUrl"])
        # Exactly one funko_pop_catalog request for the whole page, not one
        # per Funko row.
        funko_requests = [r for r in requests if "funko_pop_catalog" in str(r.url)]
        self.assertEqual(len(funko_requests), 1)

    def _pokemon_handler(self, *, list_rows: list, tcgplayer_rows: dict, sibling_rows: dict):
        # tcgplayer_pokemon_catalog and the sibling-variant check both hit
        # our own Supabase project, same as the main pricecharting_catalog
        # browse query -- distinguish by path/params, not a separate
        # external client (there is no more external client in this path).
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "tcgplayer_pokemon_catalog" in url:
                group_name = request.url.params.get("group_name", "").removeprefix("eq.")
                card_number = request.url.params.get("card_number", "").removeprefix("eq.")
                return httpx.Response(
                    200, json=tcgplayer_rows.get((group_name, card_number), [])
                )
            if "product_name" in request.url.params:
                # The sibling-check query: select=pricecharting_id only,
                # filtered by console_name + product_name ilike suffix.
                console_name = request.url.params.get("console_name", "").removeprefix("eq.")
                return httpx.Response(200, json=sibling_rows.get(console_name, []))
            return httpx.Response(200, json=list_rows)

        return handler

    def test_service_uses_plain_image_when_no_sibling_variant_rows(self) -> None:
        list_rows = [
            {
                "pricecharting_id": "630417",
                "product_name": "Charizard #4",
                "category": "Pokemon Card",
                "console_name": "Pokemon Base Set",
                "loose_price_cents": 16100,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]
        tcgplayer_rows = {
            ("Base Set", "4"): [
                {
                    "product_name": "Charizard",
                    "image_url": "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
                    "variant_tag": None,
                },
            ],
        }
        sibling_rows = {"Pokemon Base Set": [{"pricecharting_id": "630417"}]}

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(
                    transport=httpx.MockTransport(
                        self._pokemon_handler(
                            list_rows=list_rows,
                            tcgplayer_rows=tcgplayer_rows,
                            sibling_rows=sibling_rows,
                        )
                    )
                ),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(
            items_by_id["630417"]["imageUrl"],
            "https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg",
        )

    def test_service_suppresses_image_when_sibling_variant_rows_exist(self) -> None:
        list_rows = [
            {
                "pricecharting_id": "630417",
                "product_name": "Charizard #4",
                "category": "Pokemon Card",
                "console_name": "Pokemon Base Set",
                "loose_price_cents": 16100,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]
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
                {"pricecharting_id": "7096109"},
            ]
        }

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(
                    transport=httpx.MockTransport(
                        self._pokemon_handler(
                            list_rows=list_rows,
                            tcgplayer_rows=tcgplayer_rows,
                            sibling_rows=sibling_rows,
                        )
                    )
                ),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertIsNone(items_by_id["630417"]["imageUrl"])

    def test_service_uses_exact_shadowless_image(self) -> None:
        list_rows = [
            {
                "pricecharting_id": "715695",
                "product_name": "Charizard [Shadowless] #4",
                "category": "Pokemon Card",
                "console_name": "Pokemon Base Set",
                "loose_price_cents": 300000,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]
        tcgplayer_rows = {
            ("Base Set (Shadowless)", "4"): [
                {
                    "product_name": "Charizard",
                    "image_url": "https://tcgplayer-cdn.tcgplayer.com/product/106999_200w.jpg",
                    "variant_tag": "shadowless",
                },
            ],
        }

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(
                    transport=httpx.MockTransport(
                        self._pokemon_handler(
                            list_rows=list_rows, tcgplayer_rows=tcgplayer_rows, sibling_rows={}
                        )
                    )
                ),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(
            items_by_id["715695"]["imageUrl"],
            "https://tcgplayer-cdn.tcgplayer.com/product/106999_200w.jpg",
        )

    def test_service_skips_unmapped_pokemon_set(self) -> None:
        list_rows = [
            {
                "pricecharting_id": "3438352",
                "product_name": "Charizard [1st Edition] #103",
                "category": "Pokemon Card",
                "console_name": "Pokemon Japanese Expedition Expansion Pack",
                "loose_price_cents": 5000,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]
        tcgplayer_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "tcgplayer_pokemon_catalog" in str(request.url):
                tcgplayer_requests.append(request)
            return httpx.Response(200, json=list_rows)

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertIsNone(items_by_id["3438352"]["imageUrl"])
        self.assertEqual(len(tcgplayer_requests), 0)

    def test_service_uses_lego_image_when_words_overlap(self) -> None:
        list_rows = [
            {
                "pricecharting_id": "5873213",
                "product_name": "Altair #7322",
                "category": "LEGO Space",
                "console_name": "LEGO Space",
                "loose_price_cents": 4500,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "base_number": "7322",
                            "name": "Altair",
                            "image_url": "https://cdn.rebrickable.com/media/sets/7322-1.jpg",
                        },
                    ],
                )
            return httpx.Response(200, json=list_rows)

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(
            items_by_id["5873213"]["imageUrl"],
            "https://cdn.rebrickable.com/media/sets/7322-1.jpg",
        )

    def test_service_rejects_lego_number_match_without_word_overlap(self) -> None:
        # Real production case: "Roof Bricks #445" collides on set number
        # with Rebrickable's unrelated "Police Units" -- must not show it.
        list_rows = [
            {
                "pricecharting_id": "111",
                "product_name": "Roof Bricks #445",
                "category": "LEGO Classic",
                "console_name": "LEGO Classic",
                "loose_price_cents": 2000,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "rebrickable_lego_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "base_number": "445",
                            "name": "Police Units",
                            "image_url": "https://cdn.rebrickable.com/media/sets/445-1.jpg",
                        },
                    ],
                )
            return httpx.Response(200, json=list_rows)

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertIsNone(items_by_id["111"]["imageUrl"])

    def test_service_batches_lego_lookup_across_page(self) -> None:
        # One batched request for the whole page's distinct set numbers,
        # not one request per LEGO row -- see AdminCatalogService.
        # _enrich_lego_images.
        list_rows = [
            {
                "pricecharting_id": "1",
                "product_name": "Altair #7322",
                "category": "LEGO Space",
                "console_name": "LEGO Space",
                "loose_price_cents": 4500,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
            {
                "pricecharting_id": "2",
                "product_name": "Circus #1672",
                "category": "LEGO",
                "console_name": "LEGO",
                "loose_price_cents": 3000,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]
        lego_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "rebrickable_lego_catalog" in url:
                lego_requests.append(request)
                return httpx.Response(
                    200,
                    json=[
                        {
                            "base_number": "7322",
                            "name": "Altair",
                            "image_url": "https://cdn.rebrickable.com/media/sets/7322-1.jpg",
                        },
                        {
                            "base_number": "1672",
                            "name": "Circus",
                            "image_url": "https://cdn.rebrickable.com/media/sets/1672-1.jpg",
                        },
                    ],
                )
            return httpx.Response(200, json=list_rows)

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(
            items_by_id["1"]["imageUrl"], "https://cdn.rebrickable.com/media/sets/7322-1.jpg"
        )
        self.assertEqual(
            items_by_id["2"]["imageUrl"], "https://cdn.rebrickable.com/media/sets/1672-1.jpg"
        )
        self.assertEqual(len(lego_requests), 1)

    def test_service_uses_magic_image_matched_by_collector_number(self) -> None:
        list_rows = [
            {
                "pricecharting_id": "3773958",
                "product_name": "Cabaretti Charm [Gilded Foil] #365",
                "category": "Magic Streets of New Capenna",
                "console_name": "Magic Streets of New Capenna",
                "loose_price_cents": 5000,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "scryfall_magic_catalog" in url:
                params = request.url.params
                if "collector_number" in params and "365" in params.get("collector_number", ""):
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "collector_number": "365",
                                "image_url": "https://cards.scryfall.io/normal/gilded-charm.jpg",
                            },
                        ],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=list_rows)

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(
            items_by_id["3773958"]["imageUrl"],
            "https://cards.scryfall.io/normal/gilded-charm.jpg",
        )

    def test_service_suppresses_ambiguous_magic_name_match(self) -> None:
        list_rows = [
            {
                "pricecharting_id": "1",
                "product_name": "Forest",
                "category": "Magic Starter 1999",
                "console_name": "Magic Starter 1999",
                "loose_price_cents": 50,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "scryfall_magic_catalog" in url:
                params = request.url.params
                if "normalized_name" in params and "forest" in params.get("normalized_name", ""):
                    return httpx.Response(
                        200,
                        json=[
                            {"normalized_name": "forest", "image_url": "https://cards.scryfall.io/normal/forest-1.jpg"},
                            {"normalized_name": "forest", "image_url": "https://cards.scryfall.io/normal/forest-2.jpg"},
                        ],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=list_rows)

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertIsNone(items_by_id["1"]["imageUrl"])

    def test_service_batches_yugioh_lookup_across_page(self) -> None:
        # One batched request for the whole page's distinct set codes,
        # not one request per Yu-Gi-Oh row -- see AdminCatalogService.
        # _enrich_yugioh_images.
        list_rows = [
            {
                "pricecharting_id": "1",
                "product_name": "Where Arf Thou? SD40-JP033",
                "category": "YuGiOh Japanese Structure Deck",
                "console_name": "YuGiOh Japanese Structure Deck",
                "loose_price_cents": 500,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
            {
                "pricecharting_id": "2",
                "product_name": "Aqua Madoor LOB-027",
                "category": "YuGiOh Legend of Blue Eyes White Dragon",
                "console_name": "YuGiOh Legend of Blue Eyes White Dragon",
                "loose_price_cents": 100,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]
        yugioh_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "yugioh_catalog" in url:
                yugioh_requests.append(request)
                return httpx.Response(
                    200,
                    json=[
                        {
                            "set_code": "SD40-JP033",
                            "image_url": "https://images.ygoprodeck.com/images/cards/1.jpg",
                        },
                        {
                            "set_code": "LOB-027",
                            "image_url": "https://images.ygoprodeck.com/images/cards/2.jpg",
                        },
                    ],
                )
            return httpx.Response(200, json=list_rows)

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(
            items_by_id["1"]["imageUrl"], "https://images.ygoprodeck.com/images/cards/1.jpg"
        )
        self.assertEqual(
            items_by_id["2"]["imageUrl"], "https://images.ygoprodeck.com/images/cards/2.jpg"
        )
        self.assertEqual(len(yugioh_requests), 1)

    def test_service_batches_lorcana_lookup_across_page(self) -> None:
        # One batched request per distinct Lorcana set on the page, not
        # one request per row -- see AdminCatalogService.
        # _enrich_lorcana_images.
        list_rows = [
            {
                "pricecharting_id": "1",
                "product_name": "Ink Geyser [Foil] #119",
                "category": "Lorcana Archazia's Island",
                "console_name": "Lorcana Archazia's Island",
                "loose_price_cents": 200,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
            {
                "pricecharting_id": "2",
                "product_name": "Broken Pod #70",
                "category": "Lorcana Attack of the Vine",
                "console_name": "Lorcana Attack of the Vine",
                "loose_price_cents": 100,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]
        lorcana_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "lorcana_catalog" in url:
                lorcana_requests.append(request)
                params = request.url.params
                set_name = params.get("normalized_set_name", "").removeprefix("eq.")
                if set_name == "archazias island":
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "card_number": "119",
                                "image_url": "https://cards.lorcast.io/card/digital/normal/1.avif",
                            },
                        ],
                    )
                if set_name == "attack of the vine":
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "card_number": "70",
                                "image_url": "https://api.lorcana.ravensburger.com/images/en/set13/70.jpg",
                            },
                        ],
                    )
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=list_rows)

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(
            items_by_id["1"]["imageUrl"], "https://cards.lorcast.io/card/digital/normal/1.avif"
        )
        self.assertEqual(
            items_by_id["2"]["imageUrl"],
            "https://api.lorcana.ravensburger.com/images/en/set13/70.jpg",
        )
        self.assertEqual(len(lorcana_requests), 2)

    def test_service_matches_onepiece_plain_row(self) -> None:
        list_rows = [
            {
                "pricecharting_id": "1",
                "product_name": "Captain John OP07-082",
                "category": "One Piece 500 Years in the Future",
                "console_name": "One Piece 500 Years in the Future",
                "loose_price_cents": 100,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]
        onepiece_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "one_piece_catalog" in url:
                onepiece_requests.append(request)
                return httpx.Response(
                    200,
                    json=[
                        {
                            "card_set_id": "OP07-082",
                            "card_name": "Captain John",
                            "is_plain": True,
                            "image_url": "https://optcgapi.com/media/static/Card_Images/OP07-082.jpg",
                        },
                    ],
                )
            return httpx.Response(200, json=list_rows)

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(
            items_by_id["1"]["imageUrl"],
            "https://optcgapi.com/media/static/Card_Images/OP07-082.jpg",
        )
        self.assertEqual(len(onepiece_requests), 1)

    def test_service_matches_onepiece_exact_variant(self) -> None:
        list_rows = [
            {
                "pricecharting_id": "1",
                "product_name": "Perona [Box Topper] OP01-077",
                "category": "One Piece Romance Dawn",
                "console_name": "One Piece Romance Dawn",
                "loose_price_cents": 500,
                "currency": "USD",
                "updated_at": "2026-08-13T00:00:00Z",
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "one_piece_catalog" in url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "card_set_id": "OP01-077",
                            "card_name": "Perona",
                            "is_plain": True,
                            "image_url": "https://optcgapi.com/media/static/Card_Images/OP01-077.jpg",
                        },
                        {
                            "card_set_id": "OP01-077",
                            "card_name": "Perona (Box Topper)",
                            "is_plain": False,
                            "image_url": "https://optcgapi.com/media/static/Card_Images/OP01-077_p1.jpg",
                        },
                    ],
                )
            return httpx.Response(200, json=list_rows)

        service = AdminCatalogService(
            repository=SupabaseAdminCatalogRepository(
                supabase_url="https://supabase.test",
                service_role_key="service-role",
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            ),
        )

        payload = service.list_items(source="pricecharting", limit=100, offset=0)

        items_by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(
            items_by_id["1"]["imageUrl"],
            "https://optcgapi.com/media/static/Card_Images/OP01-077_p1.jpg",
        )

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
                        "image_url": "https://images.kicks.dev/air-force-1-white.png",
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
        self.assertEqual(
            item["imageUrl"], "https://images.kicks.dev/air-force-1-white.png"
        )

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
