import base64
import json
import unittest
from unittest.mock import patch

import httpx

from scripts.backfill_kicksdb_catalog import (
    API_RESULT_CAP,
    KicksDBCatalogClient,
    PartialKicksDBWriteError,
    RequestBudget,
    catalog_content_hash,
    dedupe_catalog_rows,
    fetch_segment,
    load_segments,
    to_catalog_history_row,
    to_catalog_row,
)


def _product(product_id: str, title: str, *, rank: int = 1, min_price: float = 100, variants=None) -> dict:
    return {
        "id": product_id,
        "title": title,
        "brand": "Nike",
        "model": "Air Jordan 1",
        "gender": "men",
        "product_type": "sneakers",
        "category": "Air Jordan",
        "secondary_category": "One",
        "image": "https://images.stockx.com/example.jpg",
        "sku": "DZ5485-100",
        "slug": title.lower().replace(" ", "-"),
        "rank": rank,
        "weekly_orders": 100,
        "link": "https://stockx.com/example",
        "min_price": min_price,
        "max_price": min_price * 2,
        "avg_price": min_price * 1.5,
        "variants": variants if variants is not None else [{"size": "10", "lowest_ask": min_price}],
    }


class ToCatalogRowTest(unittest.TestCase):
    def test_maps_core_fields_and_prices_to_cents(self) -> None:
        row = to_catalog_row(
            _product("abc-123", "Air Jordan 1", min_price=150.5),
            source_query='product_type="sneakers"',
            source_downloaded_at="2026-08-09T00:00:00Z",
        )
        assert row is not None
        self.assertEqual(row["kicksdb_id"], "abc-123")
        self.assertEqual(row["title"], "Air Jordan 1")
        self.assertEqual(row["brand"], "Nike")
        self.assertEqual(row["min_price_cents"], 15050)
        self.assertEqual(row["max_price_cents"], 30100)
        self.assertEqual(row["source_query"], 'product_type="sneakers"')
        self.assertEqual(len(row["variants"]), 1)
        self.assertIn("content_hash", row)

    def test_skips_rows_missing_id_or_title(self) -> None:
        self.assertIsNone(to_catalog_row({"title": "X"}, source_query="q", source_downloaded_at="t"))
        self.assertIsNone(to_catalog_row({"id": "1"}, source_query="q", source_downloaded_at="t"))

    def test_content_hash_changes_when_price_changes(self) -> None:
        row_a = to_catalog_row(_product("1", "Shoe", min_price=100), source_query="q", source_downloaded_at="t")
        row_b = to_catalog_row(_product("1", "Shoe", min_price=200), source_query="q", source_downloaded_at="t")
        self.assertNotEqual(row_a["content_hash"], row_b["content_hash"])

    def test_content_hash_stable_for_identical_data(self) -> None:
        row_a = to_catalog_row(_product("1", "Shoe"), source_query="q", source_downloaded_at="t1")
        row_b = to_catalog_row(_product("1", "Shoe"), source_query="q", source_downloaded_at="t2")
        # source_downloaded_at differs but shouldn't affect the content hash
        self.assertEqual(row_a["content_hash"], row_b["content_hash"])


class DedupeCatalogRowsTest(unittest.TestCase):
    def test_keeps_last_occurrence_per_kicksdb_id(self) -> None:
        rows = [
            {"kicksdb_id": "1", "title": "First"},
            {"kicksdb_id": "2", "title": "Second"},
            {"kicksdb_id": "1", "title": "First (updated)"},
        ]
        deduped = dedupe_catalog_rows(rows)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(
            {row["kicksdb_id"]: row["title"] for row in deduped},
            {"1": "First (updated)", "2": "Second"},
        )

    def test_skips_rows_without_an_id(self) -> None:
        self.assertEqual(dedupe_catalog_rows([{"title": "No id"}]), [])


class FetchSegmentTest(unittest.TestCase):
    def test_stops_when_meta_total_reached(self) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            page = int(request.url.params["page"])
            products = [_product(str(page), f"Shoe {page}")]
            return httpx.Response(200, json={"data": products, "meta": {"total": 2}})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        budget = RequestBudget(limit=10)
        segment = {"label": "test", "filters": 'product_type="sneakers"', "sort": "rank"}

        products, requests_used = fetch_segment(
            http, api_base="https://api.kicks.dev", api_key="tok", segment=segment,
            page_size=2, request_budget=budget,
        )

        # meta.total=2 with page_size=2 means page 1 alone satisfies the cap.
        self.assertEqual(requests_used, 1)
        self.assertEqual(len(products), 1)
        self.assertEqual(budget.used, 1)

    def test_stops_when_a_page_returns_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            if page == 1:
                return httpx.Response(200, json={"data": [_product("1", "Shoe")], "meta": {"total": 1000}})
            return httpx.Response(200, json={"data": [], "meta": {"total": 1000}})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        budget = RequestBudget(limit=10)
        segment = {"label": "test", "filters": 'product_type="sneakers"', "sort": "rank"}

        products, requests_used = fetch_segment(
            http, api_base="https://api.kicks.dev", api_key="tok", segment=segment,
            page_size=100, request_budget=budget,
        )

        self.assertEqual(len(products), 1)
        self.assertEqual(requests_used, 2)

    def test_stops_early_when_request_budget_exhausted(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [_product("1", "Shoe")], "meta": {"total": 1000}})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        budget = RequestBudget(limit=2)
        segment = {"label": "test", "filters": 'product_type="sneakers"', "sort": "rank"}

        products, requests_used = fetch_segment(
            http, api_base="https://api.kicks.dev", api_key="tok", segment=segment,
            page_size=1, request_budget=budget,
        )

        self.assertEqual(requests_used, 2)
        self.assertTrue(budget.exhausted)

    def test_max_pages_bounded_by_api_result_cap(self) -> None:
        # No meta.total signal to stop on, and a very small page size --
        # must still stop at the documented 1,000-result cap, not loop
        # forever burning requests.
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            return httpx.Response(200, json={"data": [_product(str(page), f"Shoe {page}")], "meta": {}})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        budget = RequestBudget(limit=100_000)
        segment = {"label": "test", "filters": 'product_type="sneakers"', "sort": "rank"}

        products, requests_used = fetch_segment(
            http, api_base="https://api.kicks.dev", api_key="tok", segment=segment,
            page_size=1, request_budget=budget,
        )

        self.assertEqual(requests_used, API_RESULT_CAP)


class LoadSegmentsTest(unittest.TestCase):
    def test_returns_default_when_no_json_given(self) -> None:
        segments = load_segments(None)
        labels = {segment["label"] for segment in segments}
        # The global catch-all must stay first: it is the only segment that
        # can reach a brand or gender value not enumerated below.
        self.assertEqual(segments[0]["label"], "sneakers-by-rank")
        self.assertIn("nike-by-rank", labels)
        self.assertIn("vans-by-rank", labels)

    def test_capped_brands_are_split_across_every_gender(self) -> None:
        # A brand query maxes out at KicksDB's 1,000-result cap, so the big
        # brands are additionally split by gender. Splitting on an
        # incomplete gender list would silently drop products -- Jordan
        # alone has 1,000+ in each of men/women/kids -- so assert all four
        # windows exist, not just men/women.
        labels = {segment["label"] for segment in load_segments(None)}
        for gender in ("men", "women", "kids", "unisex"):
            self.assertIn(f"jordan-{gender}-by-rank", labels)

    def test_brands_that_fit_under_the_cap_get_a_single_segment(self) -> None:
        labels = {segment["label"] for segment in load_segments(None)}
        # Previously absent entirely (only 5 of Yeezy's 122 products were
        # stored, because it relied on charting in the global top-1,000).
        self.assertIn("yeezy-by-rank", labels)
        self.assertNotIn("yeezy-men-by-rank", labels)

    def test_every_default_segment_is_unique_and_well_formed(self) -> None:
        segments = load_segments(None)
        labels = [segment["label"] for segment in segments]
        self.assertEqual(len(labels), len(set(labels)), "duplicate segment label")
        for segment in segments:
            self.assertTrue(segment["filters"].startswith('product_type="sneakers"'))
            self.assertEqual(segment["sort"], "rank")

    def test_parses_custom_segments_json(self) -> None:
        segments = load_segments(json.dumps([{"label": "nike", "filters": 'brand="Nike"'}]))
        self.assertEqual(segments, [{"label": "nike", "filters": 'brand="Nike"'}])

    def test_rejects_non_array_json(self) -> None:
        with self.assertRaises(SystemExit):
            load_segments(json.dumps({"label": "nike"}))


class ToCatalogHistoryRowTest(unittest.TestCase):
    def test_builds_current_version_from_catalog_row(self) -> None:
        catalog_row = to_catalog_row(
            _product("1", "Shoe"), source_query="q", source_downloaded_at="2026-08-09T00:00:00Z"
        )
        history_row = to_catalog_history_row(catalog_row)
        self.assertEqual(history_row["kicksdb_id"], "1")
        self.assertTrue(history_row["is_current"])
        self.assertIsNone(history_row["valid_to"])
        self.assertEqual(history_row["change_hash"], catalog_row["content_hash"])
        self.assertEqual(history_row["valid_from"], "2026-08-09T00:00:00Z")


class KicksDBCatalogClientTest(unittest.TestCase):
    def test_upsert_continues_past_a_failing_subbatch(self) -> None:
        rows = [
            to_catalog_row(_product("1", "First"), source_query="q", source_downloaded_at="t"),
            to_catalog_row(_product("2", "Second"), source_query="q", source_downloaded_at="t"),
            to_catalog_row(_product("3", "Third"), source_query="q", source_downloaded_at="t"),
        ]
        transport = _FakeTransport(current_catalog_rows=[], fail_on_post_call_index=1)
        with patch("scripts.backfill_kicksdb_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = KicksDBCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            with self.assertRaises(PartialKicksDBWriteError) as context:
                client.upsert_catalog_rows(rows, batch_size=1)

        exc = context.exception
        self.assertEqual(exc.succeeded_count, 2)
        self.assertEqual(exc.failed_ids, ["2"])
        self.assertEqual(
            [row["kicksdb_id"] for row in transport.upserted_rows],
            ["1", "3"],
        )

    def test_rejects_a_non_service_role_key(self) -> None:
        with self.assertRaises(SystemExit):
            KicksDBCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("anon"),
                timeout_seconds=1,
            )


class _FakeResponse:
    def __init__(self, payload=None) -> None:
        self._payload = [] if payload is None else payload
        self.status_code = 200
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FailingResponse:
    def __init__(self) -> None:
        self.status_code = 500
        self.text = "error"

    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError("failed", request=None, response=self)


class _FakeTransport:
    def __init__(self, *, current_catalog_rows, fail_on_post_call_index=None) -> None:
        self.current_catalog_rows = current_catalog_rows
        self.upserted_rows: list[dict] = []
        self._fail_on_post_call_index = fail_on_post_call_index
        self._post_call_count = 0

    def get(self, url: str, **kwargs):
        return _FakeResponse(self.current_catalog_rows)

    def post(self, url: str, **kwargs):
        call_index = self._post_call_count
        self._post_call_count += 1
        if call_index == self._fail_on_post_call_index:
            return _FailingResponse()
        rows = kwargs.get("json", [])
        self.upserted_rows.extend(rows)
        return _FakeResponse()

    def patch(self, url: str, **kwargs):
        return _FakeResponse()


def _fake_supabase_jwt(role: str) -> str:
    header = _b64_json({"alg": "HS256", "typ": "JWT"})
    payload = _b64_json({"role": role})
    return f"{header}.{payload}.signature"


def _b64_json(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return encoded.rstrip("=")


if __name__ == "__main__":
    unittest.main()
