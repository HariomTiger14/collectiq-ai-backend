import unittest
from datetime import datetime, timezone

import httpx

from scripts.refresh_tracked_catalog_items import (
    BULK_REFRESHED_SOURCE_FILES,
    TrackedCatalogReader,
    _stale_cutoff_iso,
    fetch_product,
    refresh_candidates,
    source_site_for,
)


def _product(product_id: str, name: str, console: str = "Baseball Cards 1962 Bazooka") -> dict:
    return {
        "id": product_id,
        "product-name": name,
        "console-name": console,
        "loose-price": 1000,
    }


class SourceSiteForTest(unittest.TestCase):
    def test_sportscardspro_prefix_routes_to_sportscardspro(self) -> None:
        self.assertEqual(source_site_for("sportscardspro-set-backfill"), "sportscardspro")
        self.assertEqual(source_site_for("sportscardspro-api-search"), "sportscardspro")

    def test_everything_else_routes_to_pricecharting(self) -> None:
        self.assertEqual(source_site_for("pricecharting-set-backfill"), "pricecharting")
        self.assertEqual(source_site_for("video_games.csv"), "pricecharting")
        self.assertEqual(source_site_for(""), "pricecharting")


class StaleCutoffIsoTest(unittest.TestCase):
    def test_subtracts_hours_from_the_given_reference_time(self) -> None:
        now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
        cutoff = _stale_cutoff_iso(24, now=now)
        self.assertEqual(cutoff, "2026-08-08T12:00:00+00:00")


class FetchProductTest(unittest.TestCase):
    def test_returns_payload_on_success(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["id"], "42")
            self.assertEqual(request.url.params["t"], "tok")
            return httpx.Response(200, json={"status": "success", **_product("42", "Card")})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        result = fetch_product(http, base_url="https://example.test", token="tok", pricecharting_id="42")

        self.assertEqual(result["id"], "42")

    def test_returns_none_on_error_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "error", "error-message": "not found"})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        result = fetch_product(http, base_url="https://example.test", token="tok", pricecharting_id="42")

        self.assertIsNone(result)

    def test_returns_none_on_http_error(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
        result = fetch_product(http, base_url="https://example.test", token="tok", pricecharting_id="42")

        self.assertIsNone(result)


class RefreshCandidatesTest(unittest.TestCase):
    def test_routes_each_candidate_to_the_right_domain_and_builds_catalog_rows(self) -> None:
        candidates = [
            {"pricecharting_id": "1", "source_file": "comic-books-set-backfill"},
            {"pricecharting_id": "2", "source_file": "sportscardspro-set-backfill"},
        ]
        requested_hosts = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_hosts.append(request.url.host)
            product_id = request.url.params["id"]
            return httpx.Response(200, json={"status": "success", **_product(product_id, f"Card {product_id}")})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        catalog_rows, failed = refresh_candidates(
            http, candidates, token="tok", sleep_seconds=0, source_downloaded_at="2026-08-09T00:00:00Z"
        )

        self.assertEqual(failed, 0)
        self.assertEqual(len(catalog_rows), 2)
        self.assertEqual(requested_hosts, ["www.pricecharting.com", "www.sportscardspro.com"])
        self.assertEqual({row["pricecharting_id"] for row in catalog_rows}, {"1", "2"})

    def test_a_failed_fetch_is_counted_but_does_not_stop_the_run(self) -> None:
        candidates = [
            {"pricecharting_id": "1", "source_file": "comic-books-set-backfill"},
            {"pricecharting_id": "2", "source_file": "comic-books-set-backfill"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params["id"] == "1":
                return httpx.Response(403)
            return httpx.Response(200, json={"status": "success", **_product("2", "Card 2")})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        catalog_rows, failed = refresh_candidates(
            http, candidates, token="tok", sleep_seconds=0, source_downloaded_at="2026-08-09T00:00:00Z"
        )

        self.assertEqual(failed, 1)
        self.assertEqual(len(catalog_rows), 1)
        self.assertEqual(catalog_rows[0]["pricecharting_id"], "2")


class TrackedCatalogReaderTest(unittest.TestCase):
    def test_fetch_tracked_pricecharting_ids_paginates_and_dedupes(self) -> None:
        pages = [
            [{"pricecharting_id": "1"}, {"pricecharting_id": "2"}, {"pricecharting_id": "1"}],
            [],
        ]
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            page = pages[min(call_count["n"], len(pages) - 1)]
            call_count["n"] += 1
            return httpx.Response(200, json=page)

        reader = TrackedCatalogReader(
            supabase_url="https://example.supabase.co",
            service_role_key="key",
            timeout_seconds=5,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        ids = reader.fetch_tracked_pricecharting_ids()

        self.assertEqual(ids, ["1", "2"])

    def test_fetch_stale_catalog_rows_returns_empty_for_no_tracked_ids(self) -> None:
        reader = TrackedCatalogReader(
            supabase_url="https://example.supabase.co", service_role_key="key", timeout_seconds=5
        )
        rows = reader.fetch_stale_catalog_rows(
            [], exclude_source_files=BULK_REFRESHED_SOURCE_FILES, stale_before="2026-08-01T00:00:00Z", limit=10
        )
        self.assertEqual(rows, [])

    def test_fetch_stale_catalog_rows_excludes_bulk_source_files_and_respects_limit(self) -> None:
        captured_params = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_params.append(dict(request.url.params))
            return httpx.Response(
                200,
                json=[
                    {"pricecharting_id": "1", "source_file": "comic-books-set-backfill", "source_downloaded_at": None},
                ],
            )

        reader = TrackedCatalogReader(
            supabase_url="https://example.supabase.co",
            service_role_key="key",
            timeout_seconds=5,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        rows = reader.fetch_stale_catalog_rows(
            ["1"],
            exclude_source_files=BULK_REFRESHED_SOURCE_FILES,
            stale_before="2026-08-01T00:00:00Z",
            limit=5,
        )

        self.assertEqual(len(rows), 1)
        params = captured_params[0]
        for bulk_file in BULK_REFRESHED_SOURCE_FILES:
            self.assertIn(bulk_file, params["source_file"])
        self.assertTrue(params["source_file"].startswith("not.in."))
        self.assertEqual(params["limit"], "5")


if __name__ == "__main__":
    unittest.main()
