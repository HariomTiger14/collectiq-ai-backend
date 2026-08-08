import json
import unittest
from unittest.mock import patch

import httpx

from scripts.backfill_pricecharting_sets import (
    API_SEARCH_RESULT_CAP,
    CONSECUTIVE_RATE_LIMIT_ABORT_THRESHOLD,
    SupabaseRegistryOpsClient,
    _RateLimitCircuitBreaker,
    _search_products,
    chunked,
    fetch_batch_csv,
    fetch_batch_csv_with_retry,
    group_by_site,
    resolve_console_uids,
    resolve_via_api_for_small_sets,
    write_catalog_rows,
)


SET_PAGE_HTML = """
<html><body><script>
    VGPC.console_uid = "G58495";
</script></body></html>
"""


class ChunkedTest(unittest.TestCase):
    def test_splits_into_even_and_remainder_chunks(self) -> None:
        self.assertEqual(chunked([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_rejects_non_positive_size(self) -> None:
        with self.assertRaises(ValueError):
            chunked([1], 0)


class GroupBySiteTest(unittest.TestCase):
    def test_groups_rows_by_source_site(self) -> None:
        rows = [
            {"source_site": "pricecharting", "registry_id": "1"},
            {"source_site": "sportscardspro", "registry_id": "2"},
            {"source_site": "pricecharting", "registry_id": "3"},
        ]
        grouped = group_by_site(rows)
        self.assertEqual({r["registry_id"] for r in grouped["pricecharting"]}, {"1", "3"})
        self.assertEqual({r["registry_id"] for r in grouped["sportscardspro"]}, {"2"})


class ResolveConsoleUidsTest(unittest.TestCase):
    def test_skips_rows_that_already_have_a_console_uid(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": "G1"}]
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        registry_client = _ExplodingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=True
        )

        self.assertEqual(resolved, rows)
        self.assertEqual(failed, [])

    def test_resolves_and_records_newly_found_console_uids(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": None}]
        http = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SET_PAGE_HTML))
        )
        registry_client = _RecordingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=False
        )

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["console_uid"], "G58495")
        self.assertEqual(failed, [])
        self.assertEqual(len(registry_client.updated_batches), 1)
        self.assertEqual(registry_client.updated_batches[0][0]["console_uid"], "G58495")

    def test_dry_run_does_not_persist_resolved_console_uids(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": None}]
        http = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SET_PAGE_HTML))
        )
        registry_client = _ExplodingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=True
        )

        self.assertEqual(resolved[0]["console_uid"], "G58495")
        self.assertEqual(failed, [])

    def test_marks_a_page_with_no_console_uid_as_failed(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": None}]
        http = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>no id here</html>"))
        )
        registry_client = _ExplodingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=True
        )

        self.assertEqual(resolved, [])
        self.assertEqual(len(failed), 1)

    def test_marks_a_fetch_error_as_failed(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": None}]
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        registry_client = _ExplodingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=True
        )

        self.assertEqual(resolved, [])
        self.assertEqual(len(failed), 1)

    def test_concurrency_resolves_every_row_exactly_once(self) -> None:
        rows = [
            {"registry_id": str(i), "url": f"https://example.test/{i}", "console_uid": None}
            for i in range(9)
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            uid = request.url.path.strip("/")
            return httpx.Response(200, text=f'<script>VGPC.console_uid = "G{uid}";</script>')

        http = httpx.Client(transport=httpx.MockTransport(handler))
        registry_client = _RecordingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows,
            http=http,
            registry_client=registry_client,
            sleep_seconds=0,
            dry_run=False,
            max_concurrency=4,
        )

        self.assertEqual(failed, [])
        self.assertEqual(len(resolved), 9)
        self.assertEqual(
            {row["registry_id"] for row in resolved},
            {row["registry_id"] for row in rows},
        )
        self.assertEqual(
            {row["console_uid"] for row in resolved},
            {f"G{i}" for i in range(9)},
        )
        # One registry update call aggregating every lane's newly-resolved rows.
        self.assertEqual(len(registry_client.updated_batches), 1)
        self.assertEqual(len(registry_client.updated_batches[0]), 9)

    def test_circuit_breaker_stops_after_consecutive_429s_without_attempting_the_rest(
        self,
    ) -> None:
        rows = [
            {"registry_id": str(i), "url": f"https://example.test/{i}", "console_uid": None}
            for i in range(6)
        ]
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(429)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        registry_client = _ExplodingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=True
        )

        self.assertEqual(resolved, [])
        self.assertEqual(len(failed), 6)
        # Only the threshold's worth of rows should ever have hit the
        # network -- the rest must be skipped once the breaker trips,
        # not attempted and then marked failed.
        self.assertEqual(request_count, CONSECUTIVE_RATE_LIMIT_ABORT_THRESHOLD)

    def test_circuit_breaker_does_not_trip_on_a_non_429_error(self) -> None:
        rows = [
            {"registry_id": str(i), "url": f"https://example.test/{i}", "console_uid": None}
            for i in range(6)
        ]
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(404)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        registry_client = _ExplodingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=True
        )

        self.assertEqual(len(failed), 6)
        # A run of non-429 errors is not a "block" signal -- every row is
        # still attempted, the breaker never trips.
        self.assertEqual(request_count, 6)

    def test_circuit_breaker_resets_after_a_success_in_between(self) -> None:
        rows = [
            {"registry_id": str(i), "url": f"https://example.test/{i}", "console_uid": None}
            for i in range(5)
        ]
        # 429, 429, success, 429, 429 -- never 3 in a row, so the breaker
        # should never trip and every row gets attempted.
        statuses = iter([429, 429, 200, 429, 429])

        def handler(request: httpx.Request) -> httpx.Response:
            status = next(statuses)
            if status == 200:
                return httpx.Response(200, text=SET_PAGE_HTML)
            return httpx.Response(status)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        registry_client = _RecordingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=False
        )

        self.assertEqual(len(resolved), 1)
        self.assertEqual(len(failed), 4)

    def test_concurrency_with_a_single_row_does_not_spawn_a_thread_pool(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": None}]
        http = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SET_PAGE_HTML))
        )
        registry_client = _RecordingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows,
            http=http,
            registry_client=registry_client,
            sleep_seconds=0,
            dry_run=False,
            max_concurrency=5,
        )

        self.assertEqual(failed, [])
        self.assertEqual(resolved[0]["console_uid"], "G58495")


class RateLimitCircuitBreakerTest(unittest.TestCase):
    def test_not_tripped_before_the_threshold(self) -> None:
        breaker = _RateLimitCircuitBreaker(threshold=3)
        breaker.record_rate_limited()
        breaker.record_rate_limited()
        self.assertFalse(breaker.tripped)

    def test_trips_at_the_threshold(self) -> None:
        breaker = _RateLimitCircuitBreaker(threshold=3)
        breaker.record_rate_limited()
        breaker.record_rate_limited()
        breaker.record_rate_limited()
        self.assertTrue(breaker.tripped)

    def test_success_resets_the_count(self) -> None:
        breaker = _RateLimitCircuitBreaker(threshold=3)
        breaker.record_rate_limited()
        breaker.record_rate_limited()
        breaker.record_success()
        breaker.record_rate_limited()
        breaker.record_rate_limited()
        self.assertFalse(breaker.tripped)


class FetchBatchCsvTest(unittest.TestCase):
    def test_builds_comma_separated_console_uids_param(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, text="id,console-name\n1,Foo\n")

        http = httpx.Client(transport=httpx.MockTransport(handler))
        text = fetch_batch_csv(http, base_url="https://example.test", token="tok", console_uids=["G1", "G2"])

        self.assertEqual(text, "id,console-name\n1,Foo\n")
        self.assertEqual(seen["params"]["t"], "tok")
        self.assertEqual(seen["params"]["console-uids"], "G1,G2")

    def test_returns_none_on_http_error(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        text = fetch_batch_csv(http, base_url="https://example.test", token="tok", console_uids=["G1"])
        self.assertIsNone(text)


def _product(product_id: str, name: str, console: str = "Baseball Cards 1962 Bazooka") -> dict:
    return {
        "id": product_id,
        "product-name": name,
        "console-name": console,
        "loose-price": 1000,
    }


class SearchProductsTest(unittest.TestCase):
    def test_returns_products_on_success(self) -> None:
        products = [_product("1", "Card A"), _product("2", "Card B")]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        result = _search_products(http, base_url="https://example.test", token="tok", query="1962 bazooka")

        self.assertEqual(result, products)

    def test_returns_none_for_empty_query(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        result = _search_products(http, base_url="https://example.test", token="tok", query="")
        self.assertIsNone(result)

    def test_returns_none_on_error_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "error", "error-message": "bad token"})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        result = _search_products(http, base_url="https://example.test", token="tok", query="x")
        self.assertIsNone(result)

    def test_returns_none_on_http_error(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
        result = _search_products(http, base_url="https://example.test", token="tok", query="x")
        self.assertIsNone(result)


class ResolveViaApiForSmallSetsTest(unittest.TestCase):
    def test_a_small_set_succeeds_and_is_removed_from_remaining(self) -> None:
        rows = [{"registry_id": "1", "set_name": "1962 Bazooka", "source_site": "sportscardspro"}]
        products = [_product("1", "Card A"), _product("2", "Card B")]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        self.assertEqual(remaining, [])
        self.assertEqual(len(succeeded), 1)
        row, catalog_rows = succeeded[0]
        self.assertEqual(row["registry_id"], "1")
        self.assertEqual(len(catalog_rows), 2)
        self.assertEqual(catalog_rows[0]["pricecharting_id"], "1")

    def test_a_set_at_the_cap_falls_back_to_remaining(self) -> None:
        rows = [{"registry_id": "1", "set_name": "2023 Panini Prizm", "source_site": "sportscardspro"}]
        products = [_product(str(i), f"Card {i}") for i in range(API_SEARCH_RESULT_CAP)]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        # Hitting the cap is ambiguous/truncated -- must fall back to CSV,
        # not be trusted as a complete result.
        self.assertEqual(succeeded, [])
        self.assertEqual(remaining, rows)

    def test_an_empty_result_falls_back_to_remaining(self) -> None:
        rows = [{"registry_id": "1", "set_name": "nonexistent set", "source_site": "sportscardspro"}]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": []})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        self.assertEqual(succeeded, [])
        self.assertEqual(remaining, rows)

    def test_a_fetch_error_falls_back_to_remaining(self) -> None:
        rows = [{"registry_id": "1", "set_name": "1962 Bazooka", "source_site": "sportscardspro"}]
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))

        succeeded, remaining = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        self.assertEqual(succeeded, [])
        self.assertEqual(remaining, rows)

    def test_multiple_rows_are_each_evaluated_independently(self) -> None:
        rows = [
            {"registry_id": "1", "set_name": "small set", "source_site": "sportscardspro"},
            {"registry_id": "2", "set_name": "big set", "source_site": "sportscardspro"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params.get("q")
            count = 5 if query == "small set" else API_SEARCH_RESULT_CAP
            products = [_product(str(i), f"Card {i}") for i in range(count)]
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        self.assertEqual(len(succeeded), 1)
        self.assertEqual(succeeded[0][0]["registry_id"], "1")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["registry_id"], "2")


class FetchBatchCsvWithRetryTest(unittest.TestCase):
    def test_returns_on_first_success_without_retrying(self) -> None:
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(200, text="id,console-name\n1,Foo\n")

        http = httpx.Client(transport=httpx.MockTransport(handler))
        text = fetch_batch_csv_with_retry(
            http,
            base_url="https://example.test",
            token="tok",
            console_uids=["G1"],
            max_attempts=3,
            retry_sleep_seconds=0,
        )

        self.assertEqual(text, "id,console-name\n1,Foo\n")
        self.assertEqual(request_count, 1)

    def test_retries_after_a_failure_and_succeeds(self) -> None:
        statuses = iter([429, 200])
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            status = next(statuses)
            if status == 200:
                return httpx.Response(200, text="id,console-name\n1,Foo\n")
            return httpx.Response(status)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        text = fetch_batch_csv_with_retry(
            http,
            base_url="https://example.test",
            token="tok",
            console_uids=["G1"],
            max_attempts=3,
            retry_sleep_seconds=0,
        )

        self.assertEqual(text, "id,console-name\n1,Foo\n")
        self.assertEqual(request_count, 2)

    def test_gives_up_after_max_attempts(self) -> None:
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(429)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        text = fetch_batch_csv_with_retry(
            http,
            base_url="https://example.test",
            token="tok",
            console_uids=["G1"],
            max_attempts=3,
            retry_sleep_seconds=0,
        )

        self.assertIsNone(text)
        self.assertEqual(request_count, 3)


class WriteCatalogRowsTest(unittest.TestCase):
    def test_returns_true_when_both_writes_succeed(self) -> None:
        client = _FakeCatalogClient()
        result = write_catalog_rows(client, [{"pricecharting_id": "1"}], batch_size=50)

        self.assertTrue(result)
        self.assertEqual(client.history_calls, 1)
        self.assertEqual(client.upsert_calls, 1)

    def test_a_systemexit_from_the_catalog_write_does_not_propagate(self) -> None:
        client = _FakeCatalogClient(raise_on="upsert", error=SystemExit("statement timeout"))
        result = write_catalog_rows(client, [{"pricecharting_id": "1"}], batch_size=50)

        self.assertFalse(result)

    def test_a_regular_exception_from_the_catalog_write_does_not_propagate(self) -> None:
        client = _FakeCatalogClient(raise_on="history", error=RuntimeError("boom"))
        result = write_catalog_rows(client, [{"pricecharting_id": "1"}], batch_size=50)

        self.assertFalse(result)


class SupabaseRegistryOpsClientTest(unittest.TestCase):
    def test_claim_rows_queries_unclaimed_or_expired_leases_and_marks_them_claimed(self) -> None:
        transport = _FakeRegistryOpsTransport(
            claimable_rows=[
                {"registry_id": "1", "source_site": "pricecharting", "url": "https://x/1", "console_uid": None, "failure_count": 0},
            ]
        )
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryOpsClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            rows = client.claim_rows(limit=10, lease_minutes=30, worker_id="worker-1")

        self.assertEqual(len(rows), 1)
        self.assertEqual(transport.patched_claim["claimed_by"], "worker-1")
        self.assertIn("registry_id", transport.get_params["select"])
        # set_name is required by the API-search hybrid path, which queries
        # /api/products?q=<set_name> before falling back to console_uid+CSV.
        self.assertIn("set_name", transport.get_params["select"])

    def test_claim_rows_excludes_already_successful_rows(self) -> None:
        # Regression test: mark_success() nulls claimed_at on completion, so
        # without this exclusion a successfully-processed row looks
        # identical to a never-attempted one on the next claim query --
        # with priority_tier ordering, that let a handful of high-priority
        # categories perpetually re-fill every batch and starve every lower
        # -tier category (all sports-cards categories) of a single run.
        transport = _FakeRegistryOpsTransport(claimable_rows=[])
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryOpsClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            client.claim_rows(limit=10, lease_minutes=30, worker_id="worker-1")

        and_filter = transport.get_params["and"]
        self.assertIn("last_fetch_status.is.null", and_filter)
        self.assertIn("last_fetch_status.neq.success", and_filter)
        self.assertIn("claimed_at.is.null", and_filter)
        self.assertNotIn("or", transport.get_params)

    def test_claim_rows_returns_empty_without_patching_when_nothing_available(self) -> None:
        transport = _FakeRegistryOpsTransport(claimable_rows=[])
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryOpsClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            rows = client.claim_rows(limit=10, lease_minutes=30, worker_id="worker-1")

        self.assertEqual(rows, [])
        self.assertIsNone(transport.patched_claim)

    def test_mark_failure_increments_failure_count_per_row(self) -> None:
        transport = _FakeRegistryOpsTransport(claimable_rows=[])
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryOpsClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            client.mark_failure([{"registry_id": "1", "failure_count": 2}])

        self.assertEqual(transport.patch_calls[0]["json"]["failure_count"], 3)
        self.assertEqual(transport.patch_calls[0]["json"]["last_fetch_status"], "error")
        self.assertEqual(transport.patch_calls[0]["params"]["registry_id"], "eq.1")

    def test_mark_failure_patches_each_row_individually(self) -> None:
        transport = _FakeRegistryOpsTransport(claimable_rows=[])
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryOpsClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            client.mark_failure(
                [
                    {"registry_id": "1", "failure_count": 0},
                    {"registry_id": "2", "failure_count": 5},
                ]
            )

        self.assertEqual(len(transport.patch_calls), 2)
        self.assertEqual(transport.patch_calls[1]["json"]["failure_count"], 6)
        self.assertEqual(transport.patch_calls[1]["params"]["registry_id"], "eq.2")

    def test_update_console_uids_patches_each_row_with_its_own_value(self) -> None:
        transport = _FakeRegistryOpsTransport(claimable_rows=[])
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryOpsClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            client.update_console_uids(
                [
                    {"registry_id": "1", "console_uid": "G1"},
                    {"registry_id": "2", "console_uid": "G2"},
                ]
            )

        self.assertEqual(len(transport.patch_calls), 2)
        self.assertEqual(transport.patch_calls[0]["json"], {"console_uid": "G1"})
        self.assertEqual(transport.patch_calls[0]["params"]["registry_id"], "eq.1")
        self.assertEqual(transport.patch_calls[1]["json"], {"console_uid": "G2"})


class _FakeCatalogClient:
    def __init__(self, *, raise_on: str | None = None, error: Exception | None = None) -> None:
        self.raise_on = raise_on
        self.error = error
        self.history_calls = 0
        self.upsert_calls = 0

    def sync_scd2_history_rows(self, rows, *, batch_size):
        self.history_calls += 1
        if self.raise_on == "history":
            raise self.error
        return len(rows)

    def upsert_rows(self, rows, *, batch_size):
        self.upsert_calls += 1
        if self.raise_on == "upsert":
            raise self.error
        return len(rows)


class _ExplodingRegistryClient:
    def update_console_uids(self, rows):
        raise AssertionError("registry client must not be called")


class _RecordingRegistryClient:
    def __init__(self) -> None:
        self.updated_batches: list[list[dict]] = []

    def update_console_uids(self, rows):
        self.updated_batches.append(list(rows))


class _FakeRegistryOpsResponse:
    def __init__(self, payload=None) -> None:
        self._payload = [] if payload is None else payload
        self.status_code = 200
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FakeRegistryOpsTransport:
    def __init__(self, *, claimable_rows: list[dict]) -> None:
        self.claimable_rows = claimable_rows
        self.get_params: dict = {}
        self.patch_calls: list[dict] = []

    @property
    def patched_claim(self):
        return self.patch_calls[-1]["json"] if self.patch_calls else None

    def get(self, url: str, **kwargs):
        self.get_params = kwargs.get("params", {})
        return _FakeRegistryOpsResponse(self.claimable_rows)

    def patch(self, url: str, **kwargs):
        self.patch_calls.append({"params": kwargs.get("params", {}), "json": kwargs.get("json", {})})
        return _FakeRegistryOpsResponse()


if __name__ == "__main__":
    unittest.main()
