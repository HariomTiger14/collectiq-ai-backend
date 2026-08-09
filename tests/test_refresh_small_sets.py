import unittest
from datetime import datetime, timezone

import httpx

from scripts.refresh_small_sets import (
    SmallSetRegistryReader,
    _stale_cutoff_iso,
    refresh_small_sets,
)


def _product(product_id: str, name: str, console: str = "Baseball Cards 1962 Bazooka") -> dict:
    return {
        "id": product_id,
        "product-name": name,
        "console-name": console,
        "loose-price": 1000,
    }


class StaleCutoffIsoTest(unittest.TestCase):
    def test_subtracts_hours_from_the_given_reference_time(self) -> None:
        now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
        cutoff = _stale_cutoff_iso(24, now=now)
        self.assertEqual(cutoff, "2026-08-08T12:00:00+00:00")


class RefreshSmallSetsTest(unittest.TestCase):
    def test_a_small_set_is_refreshed_and_marked_checked(self) -> None:
        candidates = [
            {"registry_id": "1", "source_site": "sportscardspro", "set_name": "1962 Bazooka"},
        ]
        products = [_product("1", "Card A"), _product("2", "Card B")]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        catalog_rows, refreshed_ids, checked_ids, skipped = refresh_small_sets(
            http, candidates, token="tok", sleep_seconds=0, source_downloaded_at="2026-08-09T00:00:00Z"
        )

        self.assertEqual(refreshed_ids, ["1"])
        self.assertEqual(checked_ids, ["1"])
        self.assertEqual(skipped, 0)
        self.assertEqual(len(catalog_rows), 2)

    def test_a_set_at_the_cap_is_skipped_but_still_marked_checked(self) -> None:
        candidates = [
            {"registry_id": "1", "source_site": "pricecharting", "set_name": "2023 Panini Prizm"},
        ]
        products = [_product(str(i), f"Card {i}") for i in range(100)]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        catalog_rows, refreshed_ids, checked_ids, skipped = refresh_small_sets(
            http, candidates, token="tok", sleep_seconds=0, source_downloaded_at="2026-08-09T00:00:00Z"
        )

        # Hitting the cap is ambiguous/truncated -- must not be trusted as a
        # complete refresh, but it still counts as "checked" so tier 1
        # doesn't re-attempt it every single run.
        self.assertEqual(refreshed_ids, [])
        self.assertEqual(checked_ids, ["1"])
        self.assertEqual(skipped, 1)
        self.assertEqual(catalog_rows, [])

    def test_an_empty_result_is_skipped_but_still_marked_checked(self) -> None:
        candidates = [
            {"registry_id": "1", "source_site": "pricecharting", "set_name": "nonexistent set"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": []})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        catalog_rows, refreshed_ids, checked_ids, skipped = refresh_small_sets(
            http, candidates, token="tok", sleep_seconds=0, source_downloaded_at="2026-08-09T00:00:00Z"
        )

        self.assertEqual(refreshed_ids, [])
        self.assertEqual(checked_ids, ["1"])
        self.assertEqual(skipped, 1)

    def test_the_same_item_returned_by_two_different_sets_searches_is_deduped(self) -> None:
        # Live-confirmed bug: fuzzy text search can return an item that
        # actually belongs to a DIFFERENT set (searching "Creepshow"
        # surfaced a "Stray Dogs: Dog Days [Creepshow]" crossover item). If
        # both sets are candidates in the same run, the same
        # pricecharting_id would land in the write batch twice and violate
        # the SCD2 history table's one-current-row-per-item constraint.
        candidates = [
            {"registry_id": "1", "source_site": "pricecharting", "set_name": "Creepshow"},
            {"registry_id": "2", "source_site": "pricecharting", "set_name": "Stray Dogs"},
        ]
        shared_item = _product("999", "Stray Dogs: Dog Days [Creepshow] #1", console="Comic Books Stray Dogs")

        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params["q"]
            products = [_product("1", "Card A"), shared_item] if query == "Creepshow" else [shared_item]
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        catalog_rows, refreshed_ids, checked_ids, skipped = refresh_small_sets(
            http, candidates, token="tok", sleep_seconds=0, source_downloaded_at="2026-08-09T00:00:00Z"
        )

        self.assertEqual(refreshed_ids, ["1", "2"])
        pricecharting_ids = [row["pricecharting_id"] for row in catalog_rows]
        self.assertEqual(len(pricecharting_ids), len(set(pricecharting_ids)))
        self.assertEqual(sorted(pricecharting_ids), ["1", "999"])

    def test_routes_each_candidate_to_the_right_domain(self) -> None:
        candidates = [
            {"registry_id": "1", "source_site": "pricecharting", "set_name": "Comic Books X-Men"},
            {"registry_id": "2", "source_site": "sportscardspro", "set_name": "1962 Bazooka"},
        ]
        requested_hosts = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_hosts.append(request.url.host)
            return httpx.Response(200, json={"status": "success", "products": [_product("9", "Card")]})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        refresh_small_sets(
            http, candidates, token="tok", sleep_seconds=0, source_downloaded_at="2026-08-09T00:00:00Z"
        )

        self.assertEqual(requested_hosts, ["www.pricecharting.com", "www.sportscardspro.com"])


class SmallSetRegistryReaderTest(unittest.TestCase):
    def test_fetch_stale_success_rows_filters_and_orders_correctly(self) -> None:
        captured_params = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_params.append(dict(request.url.params))
            return httpx.Response(
                200,
                json=[{"registry_id": "1", "source_site": "pricecharting", "set_name": "X-Men"}],
            )

        reader = SmallSetRegistryReader(
            supabase_url="https://example.supabase.co",
            service_role_key="key",
            timeout_seconds=5,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        rows = reader.fetch_stale_success_rows(stale_before="2026-08-08T00:00:00Z", limit=50)

        self.assertEqual(len(rows), 1)
        params = captured_params[0]
        self.assertEqual(params["last_fetch_status"], "eq.success")
        self.assertIn("tier1_refreshed_at.is.null", params["or"])
        self.assertIn("tier1_refreshed_at.lt.2026-08-08T00:00:00Z", params["or"])
        self.assertEqual(params["limit"], "50")

    def test_fetch_stale_success_rows_returns_empty_for_non_positive_limit(self) -> None:
        reader = SmallSetRegistryReader(
            supabase_url="https://example.supabase.co", service_role_key="key", timeout_seconds=5
        )
        rows = reader.fetch_stale_success_rows(stale_before="2026-08-08T00:00:00Z", limit=0)
        self.assertEqual(rows, [])

    def test_mark_tier1_checked_patches_matching_registry_ids(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200)

        reader = SmallSetRegistryReader(
            supabase_url="https://example.supabase.co",
            service_role_key="key",
            timeout_seconds=5,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        reader.mark_tier1_checked(["1", "2"])

        self.assertEqual(captured["params"]["registry_id"], "in.(1,2)")

    def test_mark_tier1_checked_is_a_noop_for_empty_ids(self) -> None:
        reader = SmallSetRegistryReader(
            supabase_url="https://example.supabase.co", service_role_key="key", timeout_seconds=5
        )
        reader.mark_tier1_checked([])  # must not raise or attempt a request


if __name__ == "__main__":
    unittest.main()
