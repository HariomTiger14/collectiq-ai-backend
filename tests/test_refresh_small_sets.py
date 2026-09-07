import unittest
from datetime import datetime, timezone

import httpx

from scripts.tier1_eligibility import MISS_THRESHOLD
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
        result = refresh_small_sets(
            http, candidates, token="tok", sleep_seconds=0, source_downloaded_at="2026-08-09T00:00:00Z"
        )

        self.assertEqual(result.refreshed_ids, ["1"])
        self.assertEqual(result.checked_ids, ["1"])
        self.assertEqual(result.skipped, 0)
        self.assertEqual(len(result.catalog_rows), 2)

    def test_a_set_at_the_cap_is_skipped_but_still_marked_checked(self) -> None:
        candidates = [
            {"registry_id": "1", "source_site": "pricecharting", "set_name": "2023 Panini Prizm"},
        ]
        products = [_product(str(i), f"Card {i}") for i in range(100)]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        result = refresh_small_sets(
            http, candidates, token="tok", sleep_seconds=0, source_downloaded_at="2026-08-09T00:00:00Z"
        )

        # Hitting the cap is ambiguous/truncated -- must not be trusted as a
        # complete refresh, but it still counts as "checked" so tier 1
        # doesn't re-attempt it every single run.
        self.assertEqual(result.refreshed_ids, [])
        self.assertEqual(result.checked_ids, ["1"])
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.catalog_rows, [])

    def test_an_empty_result_is_skipped_but_still_marked_checked(self) -> None:
        candidates = [
            {"registry_id": "1", "source_site": "pricecharting", "set_name": "nonexistent set"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": []})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        result = refresh_small_sets(
            http, candidates, token="tok", sleep_seconds=0, source_downloaded_at="2026-08-09T00:00:00Z"
        )

        self.assertEqual(result.refreshed_ids, [])
        self.assertEqual(result.checked_ids, ["1"])
        self.assertEqual(result.skipped, 1)

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
            # "Card A" carries the Creepshow console-name because a genuine
            # hit belongs to the set that was searched -- the default fixture
            # console is a different set entirely, which the tier-1 family
            # check now (correctly) reads as a wrong-family result.
            own_item = _product("1", "Card A", console="Comic Books Creepshow")
            products = [own_item, shared_item] if query == "Creepshow" else [shared_item]
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        result = refresh_small_sets(
            http, candidates, token="tok", sleep_seconds=0, source_downloaded_at="2026-08-09T00:00:00Z"
        )

        self.assertEqual(result.refreshed_ids, ["1", "2"])
        pricecharting_ids = [row["pricecharting_id"] for row in result.catalog_rows]
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




class Tier1EligibilityIntegrationTest(unittest.TestCase):
    """The loop's side of Task 9: reasons recorded, nothing written to tier-3."""

    def _run(self, products, *, set_name="1962 Bazooka", miss_count=0, status=200):
        def handler(request: httpx.Request) -> httpx.Response:
            if status != 200:
                return httpx.Response(status, json={"status": "error"})
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        return refresh_small_sets(
            http,
            [{
                "registry_id": "1", "source_site": "pricecharting",
                "set_name": set_name, "tier1_miss_count": miss_count,
            }],
            token="tok", sleep_seconds=0, source_downloaded_at="2026-09-08T00:00:00Z",
        )

    def test_a_capped_result_is_marked_ineligible_immediately(self) -> None:
        result = self._run([_product(str(i), f"Card {i}") for i in range(100)])
        self.assertEqual(result.ineligible_reasons, {"api_capped_100": 1})
        self.assertIs(result.eligibility_updates["1"]["tier1_refresh_eligible"], False)
        self.assertEqual(result.refreshed_ids, [])

    def test_a_wrong_family_result_is_marked_and_not_written(self) -> None:
        """Previously these WOULD have been written -- under 100 results and
        non-empty passed every check, so another set's prices could land
        under this set's name."""
        result = self._run(
            [_product("1", "Card A", console="Football Cards 2023 Panini Select")],
            set_name="2023 Panini Certified",
        )
        self.assertEqual(result.ineligible_reasons, {"api_ambiguous_or_wrong_family": 1})
        self.assertEqual(result.catalog_rows, [])
        self.assertEqual(result.refreshed_ids, [])

    def test_a_first_empty_result_only_counts_toward_the_threshold(self) -> None:
        result = self._run([])
        self.assertEqual(result.ineligible_reasons, {"api_empty": 1})
        self.assertEqual(result.eligibility_updates["1"], {"tier1_miss_count": 1})

    def test_a_repeated_miss_crosses_the_threshold(self) -> None:
        result = self._run([], miss_count=MISS_THRESHOLD - 1)
        self.assertIs(result.eligibility_updates["1"]["tier1_refresh_eligible"], False)

    def test_a_successful_refresh_records_no_reason(self) -> None:
        result = self._run([_product("1", "Card A"), _product("2", "Card B")])
        self.assertEqual(result.ineligible_reasons, {})
        self.assertEqual(result.refreshed_ids, ["1"])
        self.assertEqual(result.eligibility_updates, {})

    def test_a_success_after_misses_clears_the_exclusion(self) -> None:
        result = self._run([_product("1", "Card A")], miss_count=2)
        self.assertIs(result.eligibility_updates["1"]["tier1_refresh_eligible"], True)
        self.assertEqual(result.eligibility_updates["1"]["tier1_miss_count"], 0)

    def test_no_tier3_column_is_ever_written_by_the_loop(self) -> None:
        for products, name, misses in (
            ([], "1962 Bazooka", 0),
            ([_product(str(i), f"C{i}") for i in range(100)], "1962 Bazooka", 0),
            ([_product("1", "A", console="Football Cards Other")], "2023 Panini Certified", 0),
            ([], "1962 Bazooka", MISS_THRESHOLD - 1),
        ):
            with self.subTest(set_name=name, misses=misses):
                result = self._run(products, set_name=name, miss_count=misses)
                for update in result.eligibility_updates.values():
                    offenders = [k for k in update if not k.startswith("tier1_")]
                    self.assertEqual(offenders, [], f"non-tier1 column written: {offenders}")




class TransientApiFailuresDoNotPenaliseSetsTest(unittest.TestCase):
    """A throttle or outage must cost the run, not the set.

    Before this distinction every failed request became an api_404 miss, so
    three minutes of vendor 429s could have set aside a batch of perfectly
    good sets for 30 days apiece.
    """

    def _run_with_status(self, status, *, miss_count=0):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"status": "error"})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        return refresh_small_sets(
            http,
            [{
                "registry_id": "1", "source_site": "pricecharting",
                "set_name": "1962 Bazooka", "tier1_miss_count": miss_count,
            }],
            token="tok", sleep_seconds=0, source_downloaded_at="2026-09-08T00:00:00Z",
        )

    def test_a_429_writes_no_eligibility_update(self) -> None:
        result = self._run_with_status(429)
        self.assertEqual(result.eligibility_updates, {})

    def test_a_503_writes_no_eligibility_update(self) -> None:
        result = self._run_with_status(503)
        self.assertEqual(result.eligibility_updates, {})

    def test_every_transient_status_leaves_the_counter_alone(self) -> None:
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                result = self._run_with_status(status, miss_count=MISS_THRESHOLD - 1)
                self.assertEqual(
                    result.eligibility_updates, {},
                    f"HTTP {status} pushed a set toward exclusion",
                )

    def test_a_set_one_miss_from_exclusion_survives_an_outage(self) -> None:
        result = self._run_with_status(503, miss_count=MISS_THRESHOLD - 1)
        self.assertEqual(result.eligibility_updates, {})
        self.assertEqual(result.checked_ids, ["1"])

    def test_a_real_404_still_counts(self) -> None:
        """The carve-out must not disarm the case it was carved out of."""
        result = self._run_with_status(404)
        self.assertEqual(result.eligibility_updates["1"], {"tier1_miss_count": 1})

    def test_a_404_at_the_threshold_still_excludes(self) -> None:
        result = self._run_with_status(404, miss_count=MISS_THRESHOLD - 1)
        self.assertIs(result.eligibility_updates["1"]["tier1_refresh_eligible"], False)


if __name__ == "__main__":
    unittest.main()
