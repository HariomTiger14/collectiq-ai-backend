import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import httpx

from scripts.backfill_pricecharting_sets import (
    API_SEARCH_RESULT_CAP,
    CONSECUTIVE_RATE_LIMIT_ABORT_THRESHOLD,
    SPORTSCARDSPRO_API_SEARCH_MAX_IN_FLIGHT,
    SupabaseRegistryOpsClient,
    SupabaseRunLockClient,
    _Counter,
    _RateLimitCircuitBreaker,
    _redact_token,
    _StartRateLimiter,
    _build_result_summary,
    _new_api_search_counts,
    _products_match_set_name,
    _search_products,
    cap_slow_path_rows,
    chunked,
    fetch_batch_csv,
    fetch_batch_csv_with_retry,
    group_by_site,
    main,
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


class CapSlowPathRowsTest(unittest.TestCase):
    def test_returns_everything_uncapped_when_under_the_limit(self) -> None:
        rows = [{"registry_id": str(i)} for i in range(5)]
        capped, deferred = cap_slow_path_rows(rows, limit=20)
        self.assertEqual(capped, rows)
        self.assertEqual(deferred, 0)

    def test_caps_at_the_limit_and_reports_the_remainder_as_deferred(self) -> None:
        rows = [{"registry_id": str(i)} for i in range(50)]
        capped, deferred = cap_slow_path_rows(rows, limit=20)
        self.assertEqual(len(capped), 20)
        self.assertEqual(capped, rows[:20])
        self.assertEqual(deferred, 30)

    def test_a_zero_or_negative_limit_defers_everything(self) -> None:
        rows = [{"registry_id": str(i)} for i in range(5)]
        capped, deferred = cap_slow_path_rows(rows, limit=0)
        self.assertEqual(capped, [])
        self.assertEqual(deferred, 5)

    def test_empty_input_is_a_no_op(self) -> None:
        capped, deferred = cap_slow_path_rows([], limit=20)
        self.assertEqual(capped, [])
        self.assertEqual(deferred, 0)


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

    def test_records_429s_on_the_given_rate_limit_counter(self) -> None:
        rows = [
            {"registry_id": str(i), "url": f"https://example.test/{i}", "console_uid": None}
            for i in range(2)
        ]
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
        registry_client = _ExplodingRegistryClient()
        counter = _Counter()

        resolve_console_uids(
            rows,
            http=http,
            registry_client=registry_client,
            sleep_seconds=0,
            dry_run=True,
            rate_limit_counter=counter,
        )

        self.assertEqual(counter.value, 2)

    def test_does_not_record_a_non_429_error_on_the_rate_limit_counter(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": None}]
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        registry_client = _ExplodingRegistryClient()
        counter = _Counter()

        resolve_console_uids(
            rows,
            http=http,
            registry_client=registry_client,
            sleep_seconds=0,
            dry_run=True,
            rate_limit_counter=counter,
        )

        self.assertEqual(counter.value, 0)


class CounterTest(unittest.TestCase):
    def test_starts_at_zero(self) -> None:
        self.assertEqual(_Counter().value, 0)

    def test_increment_defaults_to_one(self) -> None:
        counter = _Counter()
        counter.increment()
        counter.increment()
        self.assertEqual(counter.value, 2)

    def test_increment_accepts_a_custom_amount(self) -> None:
        counter = _Counter()
        counter.increment(5)
        self.assertEqual(counter.value, 5)


class RedactTokenTest(unittest.TestCase):
    def test_replaces_the_token_with_a_placeholder(self) -> None:
        self.assertEqual(
            _redact_token("failed for url '...?t=SECRET123&q=x'", "SECRET123"),
            "failed for url '...?t=***REDACTED***&q=x'",
        )

    def test_leaves_text_unchanged_when_the_token_is_blank(self) -> None:
        self.assertEqual(_redact_token("some error text", ""), "some error text")

    def test_leaves_text_unchanged_when_the_token_does_not_appear(self) -> None:
        self.assertEqual(_redact_token("unrelated error", "SECRET123"), "unrelated error")

    def test_replaces_every_occurrence(self) -> None:
        self.assertEqual(
            _redact_token("SECRET123 appears twice: SECRET123", "SECRET123"),
            "***REDACTED*** appears twice: ***REDACTED***",
        )


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

    def test_records_a_429_on_the_given_rate_limit_counter(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
        counter = _Counter()

        text = fetch_batch_csv(
            http,
            base_url="https://example.test",
            token="tok",
            console_uids=["G1"],
            rate_limit_counter=counter,
        )

        self.assertIsNone(text)
        self.assertEqual(counter.value, 1)

    def test_does_not_record_a_non_429_error_on_the_rate_limit_counter(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        counter = _Counter()

        fetch_batch_csv(
            http,
            base_url="https://example.test",
            token="tok",
            console_uids=["G1"],
            rate_limit_counter=counter,
        )

        self.assertEqual(counter.value, 0)

    def test_does_not_leak_the_token_into_logs_on_an_error(self) -> None:
        # httpx.HTTPStatusError's default message embeds the full request
        # URL, which includes ?t=<token> here -- an unredacted print would
        # put a live PRICECHARTING_API_TOKEN straight into cron logs.
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        with patch("builtins.print") as mock_print:
            fetch_batch_csv(
                http,
                base_url="https://example.test",
                token="SECRET_TOKEN_123",
                console_uids=["G1"],
            )

        printed = " ".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertNotIn("SECRET_TOKEN_123", printed)
        self.assertIn("REDACTED", printed)


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

    def test_records_rate_limit_on_the_given_breaker_on_429(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
        breaker = _RateLimitCircuitBreaker(threshold=3)

        _search_products(http, base_url="https://example.test", token="tok", query="x", breaker=breaker)
        _search_products(http, base_url="https://example.test", token="tok", query="x", breaker=breaker)
        self.assertFalse(breaker.tripped)
        _search_products(http, base_url="https://example.test", token="tok", query="x", breaker=breaker)

        self.assertTrue(breaker.tripped)

    def test_does_not_record_rate_limit_on_a_non_429_error(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        breaker = _RateLimitCircuitBreaker(threshold=3)

        for _ in range(6):
            _search_products(http, base_url="https://example.test", token="tok", query="x", breaker=breaker)

        self.assertFalse(breaker.tripped)

    def test_works_without_a_breaker(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
        result = _search_products(http, base_url="https://example.test", token="tok", query="x")
        self.assertIsNone(result)

    def test_does_not_leak_the_token_into_logs_on_an_error(self) -> None:
        # httpx.HTTPStatusError's default message embeds the full request
        # URL, which includes ?t=<token> here -- an unredacted print would
        # put a live PRICECHARTING_API_TOKEN straight into cron logs.
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
        with patch("builtins.print") as mock_print:
            _search_products(
                http, base_url="https://example.test", token="SECRET_TOKEN_123", query="x"
            )

        printed = " ".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertNotIn("SECRET_TOKEN_123", printed)
        self.assertIn("REDACTED", printed)


class ProductsMatchSetNameTest(unittest.TestCase):
    def test_a_consistent_set_of_console_names_matches(self) -> None:
        products = [
            _product("1", "Card A", console="Baseball Cards 1962 Bazooka"),
            _product("2", "Card B", console="Baseball Cards 1962 Bazooka"),
        ]
        self.assertTrue(_products_match_set_name(products, "1962 Bazooka"))

    def test_a_single_mismatched_console_name_rejects_the_whole_result(self) -> None:
        # One item resolving to a different set (a fuzzy /api/products
        # false-positive) taints the whole result -- it can no longer be
        # trusted as this set's complete, correct checklist.
        products = [
            _product("1", "Card A", console="Baseball Cards 1962 Bazooka"),
            _product("2", "Card B", console="Baseball Cards 1963 Topps"),
        ]
        self.assertFalse(_products_match_set_name(products, "1962 Bazooka"))

    def test_an_unrelated_console_name_does_not_match(self) -> None:
        products = [_product("1", "Stray Dogs: Dog Days [Creepshow]", console="Movies")]
        self.assertFalse(_products_match_set_name(products, "Creepshow"))

    def test_empty_products_do_not_match(self) -> None:
        self.assertFalse(_products_match_set_name([], "1962 Bazooka"))

    def test_a_blank_set_name_does_not_match(self) -> None:
        products = [_product("1", "Card A", console="Baseball Cards 1962 Bazooka")]
        self.assertFalse(_products_match_set_name(products, ""))


class ResolveViaApiForSmallSetsTest(unittest.TestCase):
    def test_a_small_set_succeeds_and_is_removed_from_remaining(self) -> None:
        rows = [{"registry_id": "1", "set_name": "1962 Bazooka", "source_site": "sportscardspro"}]
        products = [_product("1", "Card A"), _product("2", "Card B")]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        self.assertEqual(remaining, [])
        self.assertEqual(len(succeeded), 1)
        row, catalog_rows = succeeded[0]
        self.assertEqual(row["registry_id"], "1")
        self.assertEqual(len(catalog_rows), 2)
        self.assertEqual(catalog_rows[0]["pricecharting_id"], "1")
        self.assertEqual(counts, {"attempted": 1, "succeeded": 1, "rejected_ambiguous": 0, "hit_cap": 0, "empty": 0})

    def test_a_set_with_mismatched_console_names_is_rejected_as_ambiguous(self) -> None:
        # Mirrors the real "Creepshow" false-positive noted in
        # refresh_small_sets.py: a fuzzy /api/products search can bleed in
        # an item from a different set. That must not be silently accepted
        # as this set's complete checklist.
        rows = [{"registry_id": "1", "set_name": "1962 Bazooka", "source_site": "sportscardspro"}]
        products = [
            _product("1", "Card A", console="Baseball Cards 1962 Bazooka"),
            _product("2", "Card B", console="Baseball Cards 1963 Topps"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        self.assertEqual(succeeded, [])
        self.assertEqual(remaining, rows)
        self.assertEqual(counts["rejected_ambiguous"], 1)
        self.assertEqual(counts["succeeded"], 0)

    def test_a_set_at_the_cap_falls_back_to_remaining(self) -> None:
        rows = [{"registry_id": "1", "set_name": "2023 Panini Prizm", "source_site": "sportscardspro"}]
        products = [_product(str(i), f"Card {i}") for i in range(API_SEARCH_RESULT_CAP)]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        # Hitting the cap is ambiguous/truncated -- must fall back to CSV,
        # not be trusted as a complete result.
        self.assertEqual(succeeded, [])
        self.assertEqual(remaining, rows)
        self.assertEqual(counts["hit_cap"], 1)
        self.assertEqual(counts["rejected_ambiguous"], 0)

    def test_an_empty_result_falls_back_to_remaining(self) -> None:
        rows = [{"registry_id": "1", "set_name": "nonexistent set", "source_site": "sportscardspro"}]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": []})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        self.assertEqual(succeeded, [])
        self.assertEqual(remaining, rows)
        self.assertEqual(counts["empty"], 1)

    def test_a_fetch_error_falls_back_to_remaining(self) -> None:
        rows = [{"registry_id": "1", "set_name": "1962 Bazooka", "source_site": "sportscardspro"}]
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))

        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        self.assertEqual(succeeded, [])
        self.assertEqual(remaining, rows)
        self.assertEqual(counts["empty"], 1)

    def test_multiple_rows_are_each_evaluated_independently(self) -> None:
        rows = [
            {"registry_id": "1", "set_name": "small set", "source_site": "sportscardspro"},
            {"registry_id": "2", "set_name": "big set", "source_site": "sportscardspro"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params.get("q")
            count = 5 if query == "small set" else API_SEARCH_RESULT_CAP
            products = [_product(str(i), f"Card {i}", console=query) for i in range(count)]
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        self.assertEqual(len(succeeded), 1)
        self.assertEqual(succeeded[0][0]["registry_id"], "1")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["registry_id"], "2")
        self.assertEqual(
            counts,
            {"attempted": 2, "succeeded": 1, "rejected_ambiguous": 0, "hit_cap": 1, "empty": 0},
        )

    def test_concurrency_resolves_every_row_exactly_once(self) -> None:
        rows = [
            {"registry_id": str(i), "set_name": f"set {i}", "source_site": "sportscardspro"}
            for i in range(9)
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params.get("q")
            products = [_product("1", "Card A", console=query)]
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows,
            http=http,
            token="tok",
            sleep_seconds=0,
            source_downloaded_at="2026-01-01T00:00:00Z",
            max_concurrency=4,
        )

        self.assertEqual(remaining, [])
        self.assertEqual(len(succeeded), 9)
        self.assertEqual(
            {row["registry_id"] for row, _ in succeeded},
            {row["registry_id"] for row in rows},
        )
        self.assertEqual(counts["succeeded"], 9)
        self.assertEqual(counts["attempted"], 9)

    def test_circuit_breaker_stops_after_consecutive_429s_without_attempting_the_rest(self) -> None:
        rows = [
            {"registry_id": str(i), "set_name": f"set {i}", "source_site": "sportscardspro"}
            for i in range(6)
        ]
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(429)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows, http=http, token="tok", sleep_seconds=0, source_downloaded_at="2026-01-01T00:00:00Z"
        )

        self.assertEqual(succeeded, [])
        self.assertEqual(len(remaining), 6)
        self.assertEqual(request_count, CONSECUTIVE_RATE_LIMIT_ABORT_THRESHOLD)
        # Only the requests that actually hit the network count as attempted
        # -- rows skipped once the breaker trips were never attempted.
        self.assertEqual(counts["attempted"], CONSECUTIVE_RATE_LIMIT_ABORT_THRESHOLD)

    def test_records_429s_on_the_given_rate_limit_counter(self) -> None:
        rows = [
            {"registry_id": str(i), "set_name": f"set {i}", "source_site": "sportscardspro"}
            for i in range(2)
        ]
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
        counter = _Counter()

        resolve_via_api_for_small_sets(
            rows,
            http=http,
            token="tok",
            sleep_seconds=0,
            source_downloaded_at="2026-01-01T00:00:00Z",
            rate_limit_counter=counter,
        )

        self.assertEqual(counter.value, 2)

    def test_concurrency_with_a_single_row_does_not_spawn_a_thread_pool(self) -> None:
        rows = [{"registry_id": "1", "set_name": "1962 Bazooka", "source_site": "sportscardspro"}]
        products = [_product("1", "Card A")]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows,
            http=http,
            token="tok",
            sleep_seconds=0,
            source_downloaded_at="2026-01-01T00:00:00Z",
            max_concurrency=5,
        )

        self.assertEqual(remaining, [])
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(counts["succeeded"], 1)


class StartRateLimiterTest(unittest.TestCase):
    def test_first_call_does_not_wait(self) -> None:
        limiter = _StartRateLimiter(0.05)
        start = time.monotonic()
        limiter.wait_for_slot()
        self.assertLess(time.monotonic() - start, 0.02)

    def test_a_second_call_waits_out_the_remaining_interval(self) -> None:
        limiter = _StartRateLimiter(0.05)
        limiter.wait_for_slot()
        start = time.monotonic()
        limiter.wait_for_slot()
        self.assertGreaterEqual(time.monotonic() - start, 0.045)

    def test_concurrent_callers_are_still_globally_spaced(self) -> None:
        # The whole point of this limiter: however many threads call it,
        # successive slot grants are still spaced by min_interval_seconds --
        # unlike per-lane pacing, there is exactly one shared clock.
        limiter = _StartRateLimiter(0.03)
        lock = threading.Lock()
        timestamps: list[float] = []

        def call() -> None:
            limiter.wait_for_slot()
            with lock:
                timestamps.append(time.monotonic())

        threads = [threading.Thread(target=call) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        timestamps.sort()
        gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.025)


class ResolveViaApiForSmallSetsOverlapTest(unittest.TestCase):
    def test_default_concurrency_never_touches_the_overlap_scheduler(self) -> None:
        rows = [
            {"registry_id": str(i), "set_name": f"set {i}", "source_site": "sportscardspro"}
            for i in range(5)
        ]
        http = httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"status": "success", "products": []})
            )
        )

        with patch(
            "scripts.backfill_pricecharting_sets._resolve_via_api_for_small_sets_overlapped"
        ) as overlapped_mock:
            resolve_via_api_for_small_sets(
                rows,
                http=http,
                token="tok",
                sleep_seconds=0,
                source_downloaded_at="2026-01-01T00:00:00Z",
            )
            overlapped_mock.assert_not_called()

    def test_explicit_concurrency_above_one_uses_the_overlap_scheduler(self) -> None:
        rows = [
            {"registry_id": str(i), "set_name": f"set {i}", "source_site": "sportscardspro"}
            for i in range(5)
        ]
        http = httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"status": "success", "products": []})
            )
        )

        with patch(
            "scripts.backfill_pricecharting_sets._resolve_via_api_for_small_sets_overlapped",
            return_value=([], [], _new_api_search_counts()),
        ) as overlapped_mock:
            resolve_via_api_for_small_sets(
                rows,
                http=http,
                token="tok",
                sleep_seconds=0,
                source_downloaded_at="2026-01-01T00:00:00Z",
                max_concurrency=2,
            )
            overlapped_mock.assert_called_once()

    def test_max_in_flight_is_clamped_to_the_safety_ceiling(self) -> None:
        rows = [
            {"registry_id": str(i), "set_name": f"set {i}", "source_site": "sportscardspro"}
            for i in range(2)
        ]
        http = httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"status": "success", "products": []})
            )
        )

        with patch(
            "scripts.backfill_pricecharting_sets._resolve_via_api_for_small_sets_overlapped",
            return_value=([], [], _new_api_search_counts()),
        ) as overlapped_mock:
            resolve_via_api_for_small_sets(
                rows,
                http=http,
                token="tok",
                sleep_seconds=0,
                source_downloaded_at="2026-01-01T00:00:00Z",
                # Well above the safety ceiling -- must still clamp, not scale.
                max_concurrency=10,
            )
            _, kwargs = overlapped_mock.call_args
            self.assertEqual(kwargs["max_in_flight"], SPORTSCARDSPRO_API_SEARCH_MAX_IN_FLIGHT)

    def test_never_exceeds_the_in_flight_ceiling_even_at_a_higher_concurrency_flag(self) -> None:
        rows = [
            {"registry_id": str(i), "set_name": f"set {i}", "source_site": "sportscardspro"}
            for i in range(10)
        ]
        lock = threading.Lock()
        active = 0
        max_active = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            query = request.url.params.get("q")
            products = [_product("1", "Card A", console=query)]
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows,
            http=http,
            token="tok",
            sleep_seconds=0,
            source_downloaded_at="2026-01-01T00:00:00Z",
            max_concurrency=10,
        )

        self.assertLessEqual(max_active, SPORTSCARDSPRO_API_SEARCH_MAX_IN_FLIGHT)
        self.assertEqual(len(succeeded), 10)
        self.assertEqual(counts["attempted"], 10)

    def test_request_starts_never_bunch_up_across_workers(self) -> None:
        rows = [
            {"registry_id": str(i), "set_name": f"set {i}", "source_site": "sportscardspro"}
            for i in range(5)
        ]
        lock = threading.Lock()
        start_times: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            with lock:
                start_times.append(time.monotonic())
            query = request.url.params.get("q")
            products = [_product("1", "Card A", console=query)]
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        resolve_via_api_for_small_sets(
            rows,
            http=http,
            token="tok",
            sleep_seconds=0.03,
            source_downloaded_at="2026-01-01T00:00:00Z",
            max_concurrency=2,
        )

        start_times.sort()
        gaps = [b - a for a, b in zip(start_times, start_times[1:])]
        # Two workers sharing one rate limiter must never start two
        # requests closer together than the configured interval -- this is
        # exactly what the old round-robin-lane concurrency could NOT
        # guarantee (independent per-lane timers can align into a burst).
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.025)

    def test_overlap_hides_slow_response_latency_compared_to_serial(self) -> None:
        rows = [
            {"registry_id": str(i), "set_name": f"set {i}", "source_site": "sportscardspro"}
            for i in range(4)
        ]

        def make_handler():
            def handler(request: httpx.Request) -> httpx.Response:
                time.sleep(0.05)
                query = request.url.params.get("q")
                products = [_product("1", "Card A", console=query)]
                return httpx.Response(200, json={"status": "success", "products": products})

            return handler

        http_serial = httpx.Client(transport=httpx.MockTransport(make_handler()))
        start = time.monotonic()
        resolve_via_api_for_small_sets(
            rows,
            http=http_serial,
            token="tok",
            sleep_seconds=0.03,
            source_downloaded_at="2026-01-01T00:00:00Z",
            max_concurrency=1,
        )
        serial_elapsed = time.monotonic() - start

        http_overlap = httpx.Client(transport=httpx.MockTransport(make_handler()))
        start = time.monotonic()
        resolve_via_api_for_small_sets(
            rows,
            http=http_overlap,
            token="tok",
            sleep_seconds=0.03,
            source_downloaded_at="2026-01-01T00:00:00Z",
            max_concurrency=2,
        )
        overlap_elapsed = time.monotonic() - start

        # Overlap must meaningfully beat serial when responses are slower
        # than the pacing interval -- that's the entire point of this mode.
        self.assertLess(overlap_elapsed, serial_elapsed * 0.85)

    def test_validation_still_rejects_ambiguous_results_under_overlap(self) -> None:
        rows = [
            {"registry_id": "1", "set_name": "1962 Bazooka", "source_site": "sportscardspro"},
            {"registry_id": "2", "set_name": "set two", "source_site": "sportscardspro"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params.get("q")
            if query == "1962 Bazooka":
                products = [
                    _product("1", "Card A", console="Baseball Cards 1962 Bazooka"),
                    _product("2", "Card B", console="Baseball Cards 1963 Topps"),
                ]
            else:
                products = [_product("3", "Card C", console=query)]
            return httpx.Response(200, json={"status": "success", "products": products})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows,
            http=http,
            token="tok",
            sleep_seconds=0,
            source_downloaded_at="2026-01-01T00:00:00Z",
            max_concurrency=2,
        )

        self.assertEqual(len(succeeded), 1)
        self.assertEqual(succeeded[0][0]["registry_id"], "2")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["registry_id"], "1")
        self.assertEqual(counts["rejected_ambiguous"], 1)

    def test_circuit_breaker_still_stops_the_run_globally_under_overlap(self) -> None:
        rows = [
            {"registry_id": str(i), "set_name": f"set {i}", "source_site": "sportscardspro"}
            for i in range(8)
        ]
        lock = threading.Lock()
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            with lock:
                request_count += 1
            return httpx.Response(429)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        succeeded, remaining, counts = resolve_via_api_for_small_sets(
            rows,
            http=http,
            token="tok",
            sleep_seconds=0,
            source_downloaded_at="2026-01-01T00:00:00Z",
            max_concurrency=2,
        )

        self.assertEqual(succeeded, [])
        self.assertEqual(len(remaining), 8)
        # Two workers can race a couple of requests past the trip point, but
        # the breaker must still cut this off well short of all 8 rows.
        self.assertLess(request_count, len(rows))
        self.assertGreaterEqual(request_count, CONSECUTIVE_RATE_LIMIT_ABORT_THRESHOLD)

    def test_records_429s_on_the_shared_rate_limit_counter_under_overlap(self) -> None:
        rows = [
            {"registry_id": str(i), "set_name": f"set {i}", "source_site": "sportscardspro"}
            for i in range(2)
        ]
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
        counter = _Counter()

        resolve_via_api_for_small_sets(
            rows,
            http=http,
            token="tok",
            sleep_seconds=0,
            source_downloaded_at="2026-01-01T00:00:00Z",
            max_concurrency=2,
            rate_limit_counter=counter,
        )

        self.assertEqual(counter.value, 2)


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

    def test_records_a_429_for_every_attempt_on_the_rate_limit_counter(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
        counter = _Counter()

        fetch_batch_csv_with_retry(
            http,
            base_url="https://example.test",
            token="tok",
            console_uids=["G1"],
            max_attempts=3,
            retry_sleep_seconds=0,
            rate_limit_counter=counter,
        )

        self.assertEqual(counter.value, 3)


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


class RunLockClientTest(unittest.TestCase):
    def test_acquire_returns_true_when_the_lock_is_free(self) -> None:
        transport = _FakeRunLockTransport(
            acquire_payload=[
                {"acquired": True, "locked_by": "worker-1", "expires_at": "2026-01-01T00:30:00Z"}
            ]
        )
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRunLockClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            acquired, held_by = client.acquire(
                lock_name="pricecharting_backfill", worker_id="worker-1", lease_seconds=1800
            )

        self.assertTrue(acquired)
        self.assertEqual(held_by, "worker-1")
        self.assertEqual(
            transport.post_calls[0]["json"],
            {
                "lock_name_arg": "pricecharting_backfill",
                "worker_id_arg": "worker-1",
                "lease_seconds_arg": 1800,
            },
        )

    def test_acquire_returns_false_when_the_lock_is_held_by_someone_else(self) -> None:
        transport = _FakeRunLockTransport(
            acquire_payload=[
                {"acquired": False, "locked_by": "other-worker", "expires_at": "2026-01-01T00:10:00Z"}
            ]
        )
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRunLockClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            acquired, held_by = client.acquire(
                lock_name="pricecharting_backfill", worker_id="worker-1", lease_seconds=1800
            )

        self.assertFalse(acquired)
        self.assertEqual(held_by, "other-worker")

    def test_acquire_handles_a_dict_payload_not_wrapped_in_a_list(self) -> None:
        transport = _FakeRunLockTransport(acquire_payload={"acquired": True, "locked_by": "worker-1"})
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRunLockClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            acquired, held_by = client.acquire(lock_name="x", worker_id="worker-1", lease_seconds=60)

        self.assertTrue(acquired)

    def test_acquire_returns_false_for_an_empty_payload(self) -> None:
        transport = _FakeRunLockTransport(acquire_payload=[])
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRunLockClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            acquired, held_by = client.acquire(lock_name="x", worker_id="worker-1", lease_seconds=60)

        self.assertFalse(acquired)
        self.assertIsNone(held_by)

    def test_acquire_raises_on_http_error(self) -> None:
        transport = _FakeRunLockTransport(acquire_status_code=500)
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRunLockClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            with self.assertRaises(SystemExit):
                client.acquire(lock_name="x", worker_id="worker-1", lease_seconds=60)

    def test_release_posts_the_lock_name_and_worker_id(self) -> None:
        transport = _FakeRunLockTransport()
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRunLockClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            client.release(lock_name="pricecharting_backfill", worker_id="worker-1")

        release_call = next(
            call for call in transport.post_calls if call["url"].endswith("release_backfill_run_lock")
        )
        self.assertEqual(
            release_call["json"],
            {"lock_name_arg": "pricecharting_backfill", "worker_id_arg": "worker-1"},
        )

    def test_release_does_not_raise_on_http_error(self) -> None:
        transport = _FakeRunLockTransport(release_status_code=500)
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRunLockClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            client.release(lock_name="pricecharting_backfill", worker_id="worker-1")


class _FakeRunLockResponse:
    def __init__(self, payload=None, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeRunLockTransport:
    def __init__(
        self,
        *,
        acquire_payload=None,
        acquire_status_code: int = 200,
        release_status_code: int = 200,
    ) -> None:
        self.acquire_payload = acquire_payload
        self.acquire_status_code = acquire_status_code
        self.release_status_code = release_status_code
        self.post_calls: list[dict] = []

    def post(self, url: str, **kwargs):
        self.post_calls.append({"url": url, "json": kwargs.get("json", {})})
        if url.endswith("acquire_backfill_run_lock"):
            return _FakeRunLockResponse(self.acquire_payload, status_code=self.acquire_status_code)
        if url.endswith("release_backfill_run_lock"):
            return _FakeRunLockResponse(None, status_code=self.release_status_code)
        raise AssertionError(f"unexpected url: {url}")


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


class BuildResultSummaryTest(unittest.TestCase):
    def _counts(self, **overrides) -> dict:
        counts = _new_api_search_counts()
        counts.update(overrides)
        return counts

    def _build(self, **overrides) -> dict:
        defaults = dict(
            dry_run=False,
            claimed_count=0,
            succeeded_count=0,
            api_search_succeeded=0,
            deferred_count=0,
            failed_count=0,
            catalog_rows_written=0,
            catalog_rows_parsed=0,
            api_search_counts=self._counts(),
            slow_path_attempted=0,
            rate_limit_429_count=0,
            phase_seconds={},
            catalog_write_phase_seconds={},
            catalog_write_events=[],
        )
        defaults.update(overrides)
        return _build_result_summary(**defaults)

    def test_counters_are_correct(self) -> None:
        summary = self._build(
            claimed_count=50,
            succeeded_count=30,
            api_search_succeeded=10,
            deferred_count=4,
            failed_count=16,
            catalog_rows_written=900,
            catalog_rows_parsed=900,
            api_search_counts=self._counts(
                attempted=17, succeeded=10, rejected_ambiguous=2, hit_cap=3, empty=2
            ),
            slow_path_attempted=12,
            rate_limit_429_count=5,
        )

        self.assertEqual(summary["claimed"], 50)
        self.assertEqual(summary["succeeded"], 30)
        self.assertEqual(summary["failed"], 16)
        self.assertEqual(summary["sportscardsproApiAttempted"], 17)
        self.assertEqual(summary["sportscardsproApiSucceeded"], 10)
        self.assertEqual(summary["sportscardsproApiRejectedAmbiguous"], 2)
        self.assertEqual(summary["sportscardsproApiHitCap"], 3)
        self.assertEqual(summary["sportscardsproApiEmpty"], 2)
        # succeeded + rejected_ambiguous + hit_cap + empty accounts for every attempted row.
        self.assertEqual(
            summary["sportscardsproApiSucceeded"]
            + summary["sportscardsproApiRejectedAmbiguous"]
            + summary["sportscardsproApiHitCap"]
            + summary["sportscardsproApiEmpty"],
            summary["sportscardsproApiAttempted"],
        )
        self.assertEqual(summary["sportscardsproSlowPathAttempted"], 12)
        self.assertEqual(summary["sportscardsproDeferred"], 4)
        self.assertEqual(summary["deferredToLaterRun"], 4)
        self.assertEqual(summary["sportscardspro429Count"], 5)

    def test_final_json_includes_phase_timings(self) -> None:
        summary = self._build(
            claimed_count=1,
            succeeded_count=1,
            catalog_rows_written=1,
            catalog_rows_parsed=1,
            phase_seconds={
                "claim": 0.125,
                "api_search": 1.5,
                "console_resolve": 12.0,
                "csv_fetch": 3.25,
                "catalog_write": 0.75,
            },
        )

        self.assertEqual(summary["claimSeconds"], 0.125)
        self.assertEqual(summary["apiSearchSeconds"], 1.5)
        self.assertEqual(summary["consoleResolveSeconds"], 12.0)
        self.assertEqual(summary["csvFetchSeconds"], 3.25)
        self.assertEqual(summary["catalogWriteSeconds"], 0.75)
        # A valid JSON encode/decode round-trip is the real contract here --
        # this is what the cron's log output actually is.
        decoded = json.loads(json.dumps(summary))
        for key in (
            "claimSeconds",
            "apiSearchSeconds",
            "consoleResolveSeconds",
            "csvFetchSeconds",
            "catalogWriteSeconds",
        ):
            self.assertIn(key, decoded)

    def test_missing_phase_seconds_default_to_zero(self) -> None:
        summary = self._build(dry_run=True)

        self.assertEqual(summary["claimSeconds"], 0.0)
        self.assertEqual(summary["apiSearchSeconds"], 0.0)
        self.assertEqual(summary["consoleResolveSeconds"], 0.0)
        self.assertEqual(summary["csvFetchSeconds"], 0.0)
        self.assertEqual(summary["catalogWriteSeconds"], 0.0)

    def test_final_json_includes_catalog_write_sub_phase_timings(self) -> None:
        summary = self._build(
            catalog_write_phase_seconds={
                "unchanged_detection": 200.5,
                "catalog_upsert": 400.25,
                "scd2_comparison": 150.0,
                "scd2_insert": 300.75,
            },
        )

        self.assertEqual(summary["unchangedDetectionSeconds"], 200.5)
        self.assertEqual(summary["catalogUpsertSeconds"], 400.25)
        self.assertEqual(summary["scd2ComparisonSeconds"], 150.0)
        self.assertEqual(summary["scd2InsertSeconds"], 300.75)

    def test_missing_catalog_write_phase_seconds_default_to_zero(self) -> None:
        # dry-run has no catalog_client at all -- main() passes {} in that case.
        summary = self._build(dry_run=True, catalog_write_phase_seconds={})

        self.assertEqual(summary["unchangedDetectionSeconds"], 0.0)
        self.assertEqual(summary["catalogUpsertSeconds"], 0.0)
        self.assertEqual(summary["scd2ComparisonSeconds"], 0.0)
        self.assertEqual(summary["scd2InsertSeconds"], 0.0)

    def test_final_json_includes_top_slowest_and_largest_catalog_writes(self) -> None:
        events = [
            {"setNames": ["Set A"], "sourceSite": "sportscardspro", "rowCount": 10, "elapsedSeconds": 0.5},
            {"setNames": ["Set B"], "sourceSite": "sportscardspro", "rowCount": 900, "elapsedSeconds": 0.1},
            {"setNames": ["Set C", "Set D"], "sourceSite": "sportscardspro", "rowCount": 50, "elapsedSeconds": 5.0},
        ]
        summary = self._build(catalog_write_events=events)

        self.assertEqual(
            [event["setNames"] for event in summary["topSlowestCatalogWrites"]],
            [["Set C", "Set D"], ["Set A"], ["Set B"]],
        )
        self.assertEqual(
            [event["setNames"] for event in summary["topLargestCatalogWrites"]],
            [["Set B"], ["Set C", "Set D"], ["Set A"]],
        )

    def test_top_catalog_writes_are_capped_at_ten(self) -> None:
        events = [
            {
                "setNames": [f"Set {i}"],
                "sourceSite": "sportscardspro",
                "rowCount": i,
                "elapsedSeconds": float(i),
            }
            for i in range(15)
        ]
        summary = self._build(catalog_write_events=events)

        self.assertEqual(len(summary["topSlowestCatalogWrites"]), 10)
        self.assertEqual(len(summary["topLargestCatalogWrites"]), 10)
        # Both lists should be the 10 largest/slowest -- i.e. 5..14, not 0..9.
        self.assertEqual(
            {event["rowCount"] for event in summary["topSlowestCatalogWrites"]},
            set(range(5, 15)),
        )

    def test_no_catalog_writes_produces_empty_top_lists(self) -> None:
        summary = self._build(catalog_write_events=[])

        self.assertEqual(summary["topSlowestCatalogWrites"], [])
        self.assertEqual(summary["topLargestCatalogWrites"], [])


class MainRunLockIntegrationTest(unittest.TestCase):
    def _argv(self, *extra: str) -> list[str]:
        return [
            "--api-token",
            "tok",
            "--supabase-url",
            "https://example.supabase.co",
            "--service-role-key",
            "key",
            *extra,
        ]

    def _json_prints(self, mock_print) -> list[dict]:
        parsed = []
        for call in mock_print.call_args_list:
            text = call.args[0] if call.args else None
            if not isinstance(text, str):
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                continue
            if isinstance(obj, dict) and "success" in obj:
                parsed.append(obj)
        return parsed

    def test_skips_the_run_and_never_claims_when_the_lock_is_held(self) -> None:
        mock_lock_client = MagicMock()
        mock_lock_client.acquire.return_value = (False, "other-worker")

        with patch(
            "scripts.backfill_pricecharting_sets.SupabaseRunLockClient",
            return_value=mock_lock_client,
        ), patch(
            "scripts.backfill_pricecharting_sets.SupabaseRegistryOpsClient"
        ) as registry_client_class, patch("builtins.print") as mock_print:
            registry_client_class.return_value.claim_rows.side_effect = AssertionError(
                "must not claim rows while the lock is held elsewhere"
            )
            exit_code = main(self._argv())

        self.assertEqual(exit_code, 0)
        mock_lock_client.acquire.assert_called_once()
        mock_lock_client.release.assert_not_called()
        summaries = self._json_prints(mock_print)
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0]["runLockHeld"])
        self.assertEqual(summaries[0]["runLockHeldBy"], "other-worker")

    def test_acquires_and_releases_the_lock_around_a_normal_run(self) -> None:
        mock_lock_client = MagicMock()
        mock_lock_client.acquire.return_value = (True, "this-worker")

        with patch(
            "scripts.backfill_pricecharting_sets.SupabaseRunLockClient",
            return_value=mock_lock_client,
        ), patch(
            "scripts.backfill_pricecharting_sets.SupabaseRegistryOpsClient"
        ) as registry_client_class, patch("builtins.print"):
            registry_client_class.return_value.claim_rows.return_value = []
            exit_code = main(self._argv())

        self.assertEqual(exit_code, 0)
        mock_lock_client.acquire.assert_called_once()
        mock_lock_client.release.assert_called_once()

    def test_releases_the_lock_even_when_the_run_raises(self) -> None:
        mock_lock_client = MagicMock()
        mock_lock_client.acquire.return_value = (True, "this-worker")

        with patch(
            "scripts.backfill_pricecharting_sets.SupabaseRunLockClient",
            return_value=mock_lock_client,
        ), patch(
            "scripts.backfill_pricecharting_sets.SupabaseRegistryOpsClient"
        ) as registry_client_class, patch("builtins.print"):
            registry_client_class.return_value.claim_rows.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                main(self._argv())

        mock_lock_client.release.assert_called_once()

    def test_skip_run_lock_flag_bypasses_locking_entirely(self) -> None:
        with patch(
            "scripts.backfill_pricecharting_sets.SupabaseRunLockClient"
        ) as lock_client_class, patch(
            "scripts.backfill_pricecharting_sets.SupabaseRegistryOpsClient"
        ) as registry_client_class, patch("builtins.print"):
            registry_client_class.return_value.claim_rows.return_value = []
            exit_code = main(self._argv("--skip-run-lock"))

        self.assertEqual(exit_code, 0)
        lock_client_class.assert_not_called()

    def test_dry_run_bypasses_locking_entirely(self) -> None:
        with patch(
            "scripts.backfill_pricecharting_sets.SupabaseRunLockClient"
        ) as lock_client_class, patch(
            "scripts.backfill_pricecharting_sets.SupabaseRegistryOpsClient"
        ) as registry_client_class, patch("builtins.print"):
            registry_client_class.return_value.claim_rows.return_value = []
            exit_code = main(self._argv("--dry-run"))

        self.assertEqual(exit_code, 0)
        lock_client_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
