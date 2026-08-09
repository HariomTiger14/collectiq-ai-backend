import base64
import json
import unittest
from unittest.mock import patch

import httpx

from scripts.discover_pricecharting_sets import (
    DEFAULT_PRIORITY_TIER,
    PRIORITY_TIER_BY_CATEGORY,
    SITE_CONFIGS,
    SupabaseRegistryClient,
    _selected_sites,
    _slugify,
    build_flat_registry_row,
    build_registry_row,
    discover_flat_category,
    discover_site,
    parse_brand_links,
    parse_set_links,
)


SEED_HTML = """
<html><body>
<nav>
  <a href="/console/nes">Nintendo NES</a>
  <a href="/brand/comic-books/marvel">Marvel Comics</a>
  <a href="/brand/comic-books/dc">DC Comics</a>
  <a href="/brand/coins/penny">Pennies</a>
  <a href="/brand/video-games/nintendo">Nintendo</a>
</nav>
</body></html>
"""

MARVEL_BRAND_HTML = """
<html><body>
<nav><a href="/console/nes">Nintendo NES</a></nav>
<div>
  <a href="/console/comic-books-amazing-spider-man">Amazing Spider-Man</a>
  <a href="/console/comic-books-x-men">X-Men</a>
  <a href="/console/comic-books-x-men">X-Men</a>
</div>
</body></html>
"""

DC_BRAND_HTML = """
<html><body>
<a href="/console/comic-books-batman">Batman</a>
</body></html>
"""


class ParseBrandLinksTest(unittest.TestCase):
    def test_filters_to_allowed_categories(self) -> None:
        links = parse_brand_links(SEED_HTML, allowed_categories={"comic-books", "coins"})
        self.assertEqual(
            sorted((link["category"], link["brand"]) for link in links),
            [("coins", "penny"), ("comic-books", "dc"), ("comic-books", "marvel")],
        )

    def test_none_allows_every_category(self) -> None:
        links = parse_brand_links(SEED_HTML, allowed_categories=None)
        categories = {link["category"] for link in links}
        self.assertIn("video-games", categories)
        self.assertIn("comic-books", categories)

    def test_ignores_non_brand_links(self) -> None:
        links = parse_brand_links(SEED_HTML, allowed_categories={"comic-books"})
        for link in links:
            self.assertNotEqual(link["brand"], "nes")


class ParseSetLinksTest(unittest.TestCase):
    def test_extracts_and_dedupes_matching_category_prefix(self) -> None:
        sets = parse_set_links(MARVEL_BRAND_HTML, category="comic-books")
        self.assertEqual(
            sorted(s["slug"] for s in sets),
            ["comic-books-amazing-spider-man", "comic-books-x-men"],
        )

    def test_ignores_links_outside_the_console_namespace(self) -> None:
        sets = parse_set_links(MARVEL_BRAND_HTML, category="comic-books")
        self.assertNotIn("nes", [s["slug"] for s in sets])

    def test_ignores_links_from_a_different_category_prefix(self) -> None:
        sets = parse_set_links(MARVEL_BRAND_HTML, category="coins")
        self.assertEqual(sets, [])


class BuildRegistryRowTest(unittest.TestCase):
    def test_builds_full_url_from_slug(self) -> None:
        row = build_registry_row(
            source_site="pricecharting",
            category="comic-books",
            brand="marvel",
            slug="comic-books-x-men",
            set_name="X-Men",
            base_url="https://www.pricecharting.com",
        )
        self.assertEqual(
            row["url"], "https://www.pricecharting.com/console/comic-books-x-men"
        )
        self.assertEqual(row["source_site"], "pricecharting")

    def test_key_set_matches_build_flat_registry_row(self) -> None:
        # PostgREST's bulk insert rejects a batch where objects have
        # different keys ("All object keys must match") -- seen live in
        # production when a 500-row batch straddled brand-crawled rows
        # (comics/coins) and flat-category rows (Lorcana/Funko/LEGO), which
        # set console_uid. Every row builder must emit the same key set.
        brand_row = build_registry_row(
            source_site="pricecharting",
            category="comic-books",
            brand="marvel",
            slug="comic-books-x-men",
            set_name="X-Men",
            base_url="https://www.pricecharting.com",
        )
        flat_row = build_flat_registry_row(
            source_site="pricecharting",
            category="lorcana-cards",
            set_name="Lorcana First Chapter",
            console_uid="G67822",
            base_url="https://www.pricecharting.com",
        )
        self.assertEqual(set(brand_row.keys()), set(flat_row.keys()))

    def test_assigns_priority_tier_by_category(self) -> None:
        coins_row = build_registry_row(
            source_site="pricecharting",
            category="coins",
            brand="penny",
            slug="coins-lincoln-wheat-penny",
            set_name="Lincoln Wheat Penny",
            base_url="https://www.pricecharting.com",
        )
        comics_row = build_registry_row(
            source_site="pricecharting",
            category="comic-books",
            brand="marvel",
            slug="comic-books-x-men",
            set_name="X-Men",
            base_url="https://www.pricecharting.com",
        )
        unknown_sport_row = build_registry_row(
            source_site="sportscardspro",
            category="cricket-cards",
            brand="topps",
            slug="cricket-cards-2025-topps",
            set_name="2025 Topps",
            base_url="https://www.sportscardspro.com",
        )
        self.assertEqual(coins_row["priority_tier"], PRIORITY_TIER_BY_CATEGORY["coins"])
        self.assertEqual(comics_row["priority_tier"], PRIORITY_TIER_BY_CATEGORY["comic-books"])
        # A sport category sportscardspro.com hasn't shown us yet (its
        # brand-page categories are discovered dynamically, not a fixed
        # list) falls back to the default rather than accidentally jumping
        # the queue ahead of already-sized categories.
        self.assertEqual(unknown_sport_row["priority_tier"], DEFAULT_PRIORITY_TIER)

    def test_assigns_priority_tiers_to_sports_categories_by_set_count(self) -> None:
        # Smallest sports categories clear first so they stop competing for
        # cron slots with the much larger ones -- see PRIORITY_TIER_BY_
        # CATEGORY's comment for why this matters (baseball was previously
        # starving the other 7 sports almost entirely via tie-break order).
        small = ["ufc-cards", "wrestling-cards", "racing-cards"]
        medium = ["soccer-cards", "hockey-cards"]
        large = ["basketball-cards", "football-cards", "baseball-cards"]
        for category in small:
            self.assertEqual(PRIORITY_TIER_BY_CATEGORY[category], 3, category)
        for category in medium:
            self.assertEqual(PRIORITY_TIER_BY_CATEGORY[category], 4, category)
        for category in large:
            self.assertEqual(PRIORITY_TIER_BY_CATEGORY[category], 5, category)
        smallest_tier = min(PRIORITY_TIER_BY_CATEGORY[c] for c in small)
        largest_tier = max(PRIORITY_TIER_BY_CATEGORY[c] for c in large)
        self.assertLess(smallest_tier, largest_tier)


class SlugifyTest(unittest.TestCase):
    def test_lowercases_and_hyphenates(self) -> None:
        self.assertEqual(_slugify("Funko Pop Marvel"), "funko-pop-marvel")

    def test_strips_apostrophes_and_punctuation(self) -> None:
        self.assertEqual(
            _slugify("Lorcana Illumineer's Quest: Deep Trouble"),
            "lorcana-illumineer-s-quest-deep-trouble",
        )

    def test_falls_back_to_set_for_empty_input(self) -> None:
        self.assertEqual(_slugify("###"), "set")


class BuildFlatRegistryRowTest(unittest.TestCase):
    def test_builds_row_with_console_uid_prepopulated(self) -> None:
        row = build_flat_registry_row(
            source_site="pricecharting",
            category="lorcana-cards",
            set_name="Lorcana First Chapter",
            console_uid="G67822",
            base_url="https://www.pricecharting.com",
        )
        self.assertEqual(row["source_site"], "pricecharting")
        self.assertEqual(row["category"], "lorcana-cards")
        self.assertEqual(row["slug"], "lorcana-cards-lorcana-first-chapter")
        self.assertEqual(row["console_uid"], "G67822")
        self.assertEqual(
            row["url"],
            "https://www.pricecharting.com/console/lorcana-cards-lorcana-first-chapter",
        )
        self.assertEqual(row["priority_tier"], PRIORITY_TIER_BY_CATEGORY["lorcana-cards"])


class DiscoverFlatCategoryTest(unittest.TestCase):
    def test_parses_autocomplete_entries_and_skips_the_all_sentinel(self) -> None:
        pages = {
            "https://example.test/consoles-autocomplete/lorcana-cards": json.dumps(
                [
                    {"label": "all", "value": ""},
                    {"label": "lorcana first chapter", "value": "G67822"},
                    {"label": "lorcana fabled", "value": "G85944"},
                ]
            ),
        }
        http = httpx.Client(transport=_FixturePageTransport(pages))
        flat = {"category": "lorcana-cards", "autocomplete_path": "/consoles-autocomplete/lorcana-cards"}

        rows = discover_flat_category(
            flat, http=http, base_url="https://example.test", source_site="pricecharting"
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["console_uid"] for row in rows}, {"G67822", "G85944"}
        )
        self.assertTrue(all(row["category"] == "lorcana-cards" for row in rows))

    def test_returns_empty_list_when_endpoint_fails(self) -> None:
        http = httpx.Client(transport=_FixturePageTransport({}))
        flat = {"category": "lorcana-cards", "autocomplete_path": "/consoles-autocomplete/lorcana-cards"}

        rows = discover_flat_category(
            flat, http=http, base_url="https://example.test", source_site="pricecharting"
        )

        self.assertEqual(rows, [])


class SelectedSitesTest(unittest.TestCase):
    def test_defaults_to_every_configured_site(self) -> None:
        sites = _selected_sites(",".join(SITE_CONFIGS))
        self.assertEqual(len(sites), len(SITE_CONFIGS))

    def test_rejects_unknown_site(self) -> None:
        with self.assertRaises(SystemExit):
            _selected_sites("ebay")


class DiscoverSiteTest(unittest.TestCase):
    def test_dry_run_crawls_brand_pages_without_calling_client(self) -> None:
        pages = {
            "https://example.test/": SEED_HTML,
            "https://example.test/brand/comic-books/marvel": MARVEL_BRAND_HTML,
            "https://example.test/brand/comic-books/dc": DC_BRAND_HTML,
        }
        http = httpx.Client(transport=_FixturePageTransport(pages))
        site = {
            "source_site": "pricecharting",
            "base_url": "https://example.test",
            "seed_path": "/",
            "categories": {"comic-books"},
        }

        summary = discover_site(
            site=site,
            http=http,
            client=_ExplodingClient(),
            dry_run=True,
            batch_size=500,
            sleep_seconds=0,
        )

        self.assertEqual(summary["brandPages"], 2)
        self.assertEqual(summary["setsFound"], 3)
        self.assertEqual(summary["setsInserted"], 0)

    def test_live_run_sends_discovered_rows_to_client(self) -> None:
        pages = {
            "https://example.test/": SEED_HTML,
            "https://example.test/brand/comic-books/marvel": MARVEL_BRAND_HTML,
            "https://example.test/brand/comic-books/dc": DC_BRAND_HTML,
        }
        http = httpx.Client(transport=_FixturePageTransport(pages))
        site = {
            "source_site": "pricecharting",
            "base_url": "https://example.test",
            "seed_path": "/",
            "categories": {"comic-books"},
        }
        client = _RecordingRegistryClient()

        summary = discover_site(
            site=site,
            http=http,
            client=client,
            dry_run=False,
            batch_size=500,
            sleep_seconds=0,
        )

        self.assertEqual(summary["setsInserted"], 3)
        self.assertEqual(len(client.batches), 1)
        slugs = {row["slug"] for row in client.batches[0]}
        self.assertEqual(
            slugs,
            {
                "comic-books-amazing-spider-man",
                "comic-books-x-men",
                "comic-books-batman",
            },
        )

    def test_skips_a_brand_page_that_fails_to_fetch(self) -> None:
        pages = {
            "https://example.test/": SEED_HTML,
            "https://example.test/brand/comic-books/marvel": MARVEL_BRAND_HTML,
        }
        http = httpx.Client(transport=_FixturePageTransport(pages, missing_status=404))
        site = {
            "source_site": "pricecharting",
            "base_url": "https://example.test",
            "seed_path": "/",
            "categories": {"comic-books"},
        }

        summary = discover_site(
            site=site,
            http=http,
            client=_ExplodingClient(),
            dry_run=True,
            batch_size=500,
            sleep_seconds=0,
        )

        self.assertEqual(summary["brandPages"], 2)
        self.assertEqual(summary["setsFound"], 2)

    def test_includes_flat_category_sets_alongside_brand_pages(self) -> None:
        pages = {
            "https://example.test/": SEED_HTML,
            "https://example.test/brand/comic-books/marvel": MARVEL_BRAND_HTML,
            "https://example.test/brand/comic-books/dc": DC_BRAND_HTML,
            "https://example.test/consoles-autocomplete/lorcana-cards": json.dumps(
                [
                    {"label": "all", "value": ""},
                    {"label": "lorcana first chapter", "value": "G67822"},
                ]
            ),
        }
        http = httpx.Client(transport=_FixturePageTransport(pages))
        site = {
            "source_site": "pricecharting",
            "base_url": "https://example.test",
            "seed_path": "/",
            "categories": {"comic-books"},
            "flat_categories": [
                {
                    "category": "lorcana-cards",
                    "autocomplete_path": "/consoles-autocomplete/lorcana-cards",
                }
            ],
        }

        summary = discover_site(
            site=site,
            http=http,
            client=_ExplodingClient(),
            dry_run=True,
            batch_size=500,
            sleep_seconds=0,
        )

        self.assertEqual(summary["flatCategories"], 1)
        self.assertEqual(summary["setsFound"], 4)


class SupabaseRegistryClientTest(unittest.TestCase):
    def test_insert_new_rows_batches_and_counts_only_newly_inserted_rows(self) -> None:
        rows = [{"source_site": "pricecharting", "slug": f"comic-books-{i}"} for i in range(5)]
        transport = _FakeRegistryTransport()
        with patch("scripts.discover_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )

            inserted = client.insert_new_rows(rows, batch_size=2)

        # Fake transport simulates one duplicate silently ignored per batch
        # (PostgREST's ignore-duplicates only returns the rows actually inserted).
        self.assertEqual([len(batch) for batch in transport.received_batches], [2, 2, 1])
        self.assertEqual(inserted, 2)

    def test_rejects_a_non_service_role_key(self) -> None:
        with self.assertRaises(SystemExit):
            SupabaseRegistryClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("anon"),
                timeout_seconds=1,
            )


class _FixturePageTransport(httpx.BaseTransport):
    def __init__(self, pages: dict[str, str], *, missing_status: int = 404) -> None:
        self._pages = pages
        self._missing_status = missing_status

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in self._pages:
            return httpx.Response(self._missing_status, text="not found")
        return httpx.Response(200, text=self._pages[url])


class _RecordingRegistryClient:
    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    def insert_new_rows(self, rows, *, batch_size):
        self.batches.append(list(rows))
        return len(rows)


class _ExplodingClient:
    def insert_new_rows(self, rows, *, batch_size):
        raise AssertionError("client must not be called during a dry run")


class _FakeRegistryResponse:
    def __init__(self, payload: list[dict]) -> None:
        self._payload = payload
        self.status_code = 201
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FakeRegistryTransport:
    def __init__(self) -> None:
        self.received_batches: list[list[dict]] = []

    def post(self, url: str, **kwargs):
        batch = kwargs.get("json", [])
        self.received_batches.append(batch)
        # Simulate the last row in every batch already existing (ignored).
        return _FakeRegistryResponse(batch[:-1])


def _fake_supabase_jwt(role: str) -> str:
    header = _b64_json({"alg": "HS256", "typ": "JWT"})
    payload = _b64_json({"role": role})
    return f"{header}.{payload}.signature"


def _b64_json(payload: dict[str, str]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return encoded.rstrip("=")


if __name__ == "__main__":
    unittest.main()
