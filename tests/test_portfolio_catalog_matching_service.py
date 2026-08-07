import json
import unittest
from unittest.mock import patch

import httpx

from app.schemas.search import (
    CatalogDetailResponse,
    CatalogHistoryPoint,
    CatalogSearchPricing,
    CatalogSearchResponse,
    CatalogSearchResult,
)
from app.services.pricing.catalog_search_service import CatalogSearchError
from app.services.pricing.portfolio_catalog_matching_service import (
    PortfolioCatalogMatchingError,
    PortfolioItemReader,
    _display_currency_from_item,
    backfill_catalog_history_for_item,
    build_match_query,
    find_best_match,
    match_unlinked_portfolio_items,
)


def _result(*, id: str, confidence: float) -> CatalogSearchResult:
    return CatalogSearchResult(
        id=id,
        title="Amazing Spider-Man #300",
        category="Comic Books",
        source="PriceCharting",
        setName="Amazing Spider-Man",
        identifier=None,
        productUrl=None,
        sourceFile=None,
        confidence=confidence,
        attribution="Pricing data by PriceCharting",
        lastUpdated=None,
        imageUrl=None,
        pricing=CatalogSearchPricing(
            currency="USD",
            marketValue=100.0,
            lowEstimate=80.0,
            highEstimate=120.0,
            loosePrice=100.0,
            cibPrice=None,
            newPrice=None,
            gradedPrice=None,
        ),
    )


class BuildMatchQueryTest(unittest.TestCase):
    def test_uses_title(self) -> None:
        self.assertEqual(build_match_query({"title": " Amazing Spider-Man #300 "}), "Amazing Spider-Man #300")

    def test_empty_without_a_title(self) -> None:
        self.assertEqual(build_match_query({"title": None}), "")
        self.assertEqual(build_match_query({}), "")


class FindBestMatchTest(unittest.TestCase):
    def test_returns_the_first_result_immediately_when_it_already_qualifies(self) -> None:
        catalog = _StubCatalog(
            CatalogSearchResponse(
                query="amazing spider-man #300",
                count=2,
                results=[_result(id="first", confidence=0.96), _result(id="second", confidence=0.96)],
            )
        )

        match = find_best_match(catalog, "Amazing Spider-Man #300")

        assert match is not None
        self.assertEqual(match["id"], "first")

    def test_score_is_an_integer_percentage_not_a_raw_float(self) -> None:
        # pricecharting_match_score is an integer column -- writing the raw
        # 0.0-1.0 confidence float crashed with a real Postgres 22P02 error
        # on a live run. Must always come back as a rounded int 0-100.
        catalog = _StubCatalog(
            CatalogSearchResponse(query="q", count=1, results=[_result(id="x", confidence=0.96)])
        )

        match = find_best_match(catalog, "q")

        assert match is not None
        self.assertEqual(match["score"], 96)
        self.assertIsInstance(match["score"], int)

    def test_skips_low_confidence_and_takes_the_next_qualifying_result(self) -> None:
        catalog = _StubCatalog(
            CatalogSearchResponse(
                query="q",
                count=2,
                results=[_result(id="skip-me", confidence=0.62), _result(id="take-me", confidence=0.90)],
            )
        )

        match = find_best_match(catalog, "q")

        assert match is not None
        self.assertEqual(match["id"], "take-me")

    def test_returns_none_when_nothing_qualifies(self) -> None:
        catalog = _StubCatalog(
            CatalogSearchResponse(query="q", count=1, results=[_result(id="x", confidence=0.62)])
        )

        self.assertIsNone(find_best_match(catalog, "q"))

    def test_returns_none_on_search_error(self) -> None:
        catalog = _ExplodingCatalog()

        self.assertIsNone(find_best_match(catalog, "q"))


class DisplayCurrencyFromItemTest(unittest.TestCase):
    def test_uses_raw_json_currency(self) -> None:
        item = {"raw_json": {"currency": "USD"}}
        self.assertEqual(_display_currency_from_item(item), "USD")

    def test_falls_back_to_nested_pricing_currency(self) -> None:
        item = {"raw_json": {"pricing": {"currency": "GBP"}}}
        self.assertEqual(_display_currency_from_item(item), "GBP")

    def test_defaults_to_aud(self) -> None:
        self.assertEqual(_display_currency_from_item({}), "AUD")
        self.assertEqual(_display_currency_from_item({"raw_json": {}}), "AUD")


def _history_point(*, valid_from: str, market_value: float | None, currency: str = "USD") -> CatalogHistoryPoint:
    return CatalogHistoryPoint(
        validFrom=valid_from,
        validTo=None,
        isCurrent=False,
        sourceFile=None,
        sourceDownloadedAt=None,
        pricing=CatalogSearchPricing(
            currency=currency,
            marketValue=market_value,
            lowEstimate=market_value * 0.9 if market_value else None,
            highEstimate=market_value * 1.1 if market_value else None,
            loosePrice=market_value,
            cibPrice=None,
            newPrice=None,
            gradedPrice=None,
        ),
    )


class BackfillCatalogHistoryForItemTest(unittest.TestCase):
    def test_inserts_only_points_before_the_earliest_existing_snapshot(self) -> None:
        history = [
            _history_point(valid_from="2026-01-01T00:00:00Z", market_value=90.0),
            _history_point(valid_from="2026-03-01T00:00:00Z", market_value=100.0),
            _history_point(valid_from="2026-06-01T00:00:00Z", market_value=110.0),
        ]
        catalog = _StubDetailCatalog(history=history)
        reader = _RecordingHistoryReader(earliest_existing="2026-05-01T00:00:00Z")
        item = {"id": "item-1", "user_id": "u1", "raw_json": {}}

        inserted = backfill_catalog_history_for_item(
            catalog,
            reader,
            item=item,
            catalog_id="12345",
            match_source="PriceCharting",
            match_confidence=0.96,
        )

        # Only the Jan and Mar points are before the May cutoff; June is after.
        self.assertEqual(inserted, 2)
        self.assertEqual(len(reader.inserted_rows), 2)
        self.assertEqual(reader.inserted_rows[0]["priced_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(reader.inserted_rows[1]["priced_at"], "2026-03-01T00:00:00Z")

    def test_inserts_everything_when_the_item_has_no_existing_snapshots(self) -> None:
        history = [_history_point(valid_from="2026-01-01T00:00:00Z", market_value=90.0)]
        catalog = _StubDetailCatalog(history=history)
        reader = _RecordingHistoryReader(earliest_existing=None)
        item = {"id": "item-1", "user_id": "u1", "raw_json": {}}

        inserted = backfill_catalog_history_for_item(
            catalog, reader, item=item, catalog_id="12345", match_source="PriceCharting", match_confidence=0.96
        )

        self.assertEqual(inserted, 1)

    def test_skips_points_with_no_real_price(self) -> None:
        history = [
            _history_point(valid_from="2026-01-01T00:00:00Z", market_value=None),
            _history_point(valid_from="2026-02-01T00:00:00Z", market_value=0),
        ]
        catalog = _StubDetailCatalog(history=history)
        reader = _RecordingHistoryReader(earliest_existing=None)
        item = {"id": "item-1", "user_id": "u1", "raw_json": {}}

        inserted = backfill_catalog_history_for_item(
            catalog, reader, item=item, catalog_id="12345", match_source="PriceCharting", match_confidence=0.96
        )

        self.assertEqual(inserted, 0)
        self.assertEqual(reader.inserted_rows, [])

    def test_converts_currency_to_the_items_display_currency(self) -> None:
        from app.services.pricing.currency_conversion import _exchange_rate

        history = [_history_point(valid_from="2026-01-01T00:00:00Z", market_value=100.0, currency="USD")]
        catalog = _StubDetailCatalog(history=history)
        reader = _RecordingHistoryReader(earliest_existing=None)
        item = {"id": "item-1", "user_id": "u1", "raw_json": {"currency": "AUD"}}

        backfill_catalog_history_for_item(
            catalog, reader, item=item, catalog_id="12345", match_source="PriceCharting", match_confidence=0.96
        )

        rate = _exchange_rate("USD", "AUD")
        self.assertEqual(reader.inserted_rows[0]["value_aud"], 100.0 * rate)

    def test_returns_zero_when_catalog_detail_lookup_fails(self) -> None:
        catalog = _ExplodingDetailCatalog()
        reader = _RecordingHistoryReader(earliest_existing=None)
        item = {"id": "item-1", "user_id": "u1", "raw_json": {}}

        inserted = backfill_catalog_history_for_item(
            catalog, reader, item=item, catalog_id="12345", match_source="PriceCharting", match_confidence=0.96
        )

        self.assertEqual(inserted, 0)
        self.assertEqual(reader.inserted_rows, [])

    def test_stamps_rows_with_catalog_lookup_strategy_and_backfill_reason(self) -> None:
        history = [_history_point(valid_from="2026-01-01T00:00:00Z", market_value=100.0, currency="AUD")]
        catalog = _StubDetailCatalog(history=history)
        reader = _RecordingHistoryReader(earliest_existing=None)
        item = {"id": "item-1", "user_id": "u1", "raw_json": {}}

        backfill_catalog_history_for_item(
            catalog, reader, item=item, catalog_id="12345", match_source="PriceCharting", match_confidence=0.96
        )

        row = reader.inserted_rows[0]
        self.assertEqual(row["valuation_strategy"], "catalog_lookup")
        self.assertEqual(row["reason_code"], "catalog_history_backfill")
        self.assertEqual(row["portfolio_item_id"], "item-1")
        self.assertEqual(row["user_id"], "u1")


class MatchUnlinkedPortfolioItemsTest(unittest.TestCase):
    def test_dry_run_does_not_write_and_reports_counts(self) -> None:
        items = [
            {"id": "1", "user_id": "u1", "title": "Amazing Spider-Man #300"},
            {"id": "2", "user_id": "u1", "title": ""},  # missing title -> skipped
        ]

        with patch(
            "app.services.pricing.portfolio_catalog_matching_service.PortfolioItemReader"
        ) as reader_cls, patch(
            "app.services.pricing.portfolio_catalog_matching_service.CatalogSearchService"
        ) as catalog_cls, patch(
            "app.services.pricing.portfolio_catalog_matching_service.settings"
        ) as settings, patch(
            "app.services.pricing.portfolio_catalog_matching_service.find_best_match"
        ) as find_best_match_fn:
            settings.supabase_url = "https://example.supabase.co"
            settings.supabase_service_role_key = "service-role"
            reader = reader_cls.return_value
            reader.fetch_unlinked_items.return_value = items
            find_best_match_fn.return_value = {"id": "123", "score": 0.96}

            result = match_unlinked_portfolio_items(limit=200, dry_run=True)

        self.assertEqual(result.candidateCount, 2)
        self.assertEqual(result.matchedCount, 1)
        self.assertEqual(result.skippedMissingTitle, 1)
        reader.apply_updates.assert_not_called()
        catalog_cls.assert_called_once()

    def test_live_run_applies_updates_for_matched_and_unmatched_items(self) -> None:
        items = [
            {"id": "1", "user_id": "u1", "title": "Amazing Spider-Man #300"},
            {"id": "2", "user_id": "u1", "title": "Some Obscure Item"},
        ]

        with patch(
            "app.services.pricing.portfolio_catalog_matching_service.PortfolioItemReader"
        ) as reader_cls, patch(
            "app.services.pricing.portfolio_catalog_matching_service.CatalogSearchService"
        ), patch(
            "app.services.pricing.portfolio_catalog_matching_service.settings"
        ) as settings, patch(
            "app.services.pricing.portfolio_catalog_matching_service.find_best_match"
        ) as find_best_match_fn, patch(
            "app.services.pricing.portfolio_catalog_matching_service.backfill_catalog_history_for_item"
        ) as backfill_fn:
            settings.supabase_url = "https://example.supabase.co"
            settings.supabase_service_role_key = "service-role"
            reader = reader_cls.return_value
            reader.fetch_unlinked_items.return_value = items
            reader.apply_updates.return_value = 0
            find_best_match_fn.side_effect = [
                {"id": "123", "score": 96, "source": "PriceCharting", "confidence": 0.96},
                None,
            ]
            backfill_fn.return_value = 3

            result = match_unlinked_portfolio_items(limit=200, dry_run=False)

        self.assertEqual(result.matchedCount, 1)
        self.assertEqual(result.unmatchedCount, 1)
        self.assertEqual(result.updateFailures, 0)
        self.assertEqual(result.historyRowsBackfilled, 3)
        backfill_fn.assert_called_once()
        self.assertEqual(backfill_fn.call_args.kwargs["catalog_id"], "123")
        reader.apply_updates.assert_called_once()
        updates = reader.apply_updates.call_args[0][0]
        self.assertEqual(updates[0]["pricecharting_id"], "123")
        self.assertNotIn("pricecharting_id", updates[1])
        self.assertIn("pricecharting_match_attempted_at", updates[1])

    def test_dry_run_does_not_backfill_history(self) -> None:
        items = [{"id": "1", "user_id": "u1", "title": "Amazing Spider-Man #300"}]

        with patch(
            "app.services.pricing.portfolio_catalog_matching_service.PortfolioItemReader"
        ) as reader_cls, patch(
            "app.services.pricing.portfolio_catalog_matching_service.CatalogSearchService"
        ), patch(
            "app.services.pricing.portfolio_catalog_matching_service.settings"
        ) as settings, patch(
            "app.services.pricing.portfolio_catalog_matching_service.find_best_match",
            return_value={"id": "123", "score": 96, "source": "PriceCharting", "confidence": 0.96},
        ), patch(
            "app.services.pricing.portfolio_catalog_matching_service.backfill_catalog_history_for_item"
        ) as backfill_fn:
            settings.supabase_url = "https://example.supabase.co"
            settings.supabase_service_role_key = "service-role"
            reader_cls.return_value.fetch_unlinked_items.return_value = items

            result = match_unlinked_portfolio_items(limit=200, dry_run=True)

        backfill_fn.assert_not_called()
        self.assertEqual(result.historyRowsBackfilled, 0)


class PortfolioItemReaderTest(unittest.TestCase):
    def test_fetch_unlinked_items_filters_by_null_pricecharting_id(self) -> None:
        captured_params: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_params.update(dict(request.url.params))
            return httpx.Response(200, json=[{"id": "1", "user_id": "u1", "title": "X"}])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reader = PortfolioItemReader(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=client,
        )

        items = reader.fetch_unlinked_items(limit=200)

        self.assertEqual(len(items), 1)
        self.assertEqual(captured_params["pricecharting_id"], "is.null")

    def test_apply_updates_patches_each_row_with_its_own_composite_key(self) -> None:
        patch_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            patch_calls.append(dict(request.url.params))
            return httpx.Response(200)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reader = PortfolioItemReader(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=client,
        )

        reader.apply_updates(
            [
                {"id": "1", "user_id": "u1", "pricecharting_id": "123"},
                {"id": "2", "user_id": "u2", "pricecharting_match_attempted_at": "now"},
            ]
        )

        self.assertEqual(len(patch_calls), 2)
        self.assertEqual(patch_calls[0]["id"], "eq.1")
        self.assertEqual(patch_calls[0]["user_id"], "eq.u1")
        self.assertEqual(patch_calls[1]["id"], "eq.2")

    def test_apply_updates_continues_past_a_failing_row_and_counts_failures(self) -> None:
        # Hit for real: one row's bad value 400'd and, before this fix,
        # aborted every other row's update in the same batch. A failure must
        # be logged and skipped, not stop the loop.
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            if "bad" in body.values():
                return httpx.Response(400, json={"message": "invalid input syntax"})
            return httpx.Response(200)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reader = PortfolioItemReader(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=client,
        )

        failures = reader.apply_updates(
            [
                {"id": "1", "user_id": "u1", "pricecharting_id": "bad"},
                {"id": "2", "user_id": "u2", "pricecharting_id": "123"},
            ]
        )

        self.assertEqual(failures, 1)

    def test_apply_updates_returns_zero_when_everything_succeeds(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        reader = PortfolioItemReader(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=client,
        )

        failures = reader.apply_updates([{"id": "1", "user_id": "u1", "pricecharting_id": "123"}])

        self.assertEqual(failures, 0)

    def test_raises_without_configuration(self) -> None:
        with self.assertRaises(PortfolioCatalogMatchingError):
            PortfolioItemReader(supabase_url="", service_role_key="")

    def test_fetch_earliest_snapshot_at_returns_the_priced_at_of_the_oldest_row(self) -> None:
        captured_params: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_params.update(dict(request.url.params))
            return httpx.Response(200, json=[{"priced_at": "2026-01-01T00:00:00Z"}])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reader = PortfolioItemReader(
            supabase_url="https://example.supabase.co", service_role_key="service-role", client=client
        )

        earliest = reader.fetch_earliest_snapshot_at(item_id="item-1", user_id="u1")

        self.assertEqual(earliest, "2026-01-01T00:00:00Z")
        self.assertEqual(captured_params["order"], "priced_at.asc")
        self.assertEqual(captured_params["portfolio_item_id"], "eq.item-1")

    def test_fetch_earliest_snapshot_at_returns_none_when_no_snapshots_exist(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
        reader = PortfolioItemReader(
            supabase_url="https://example.supabase.co", service_role_key="service-role", client=client
        )

        self.assertIsNone(reader.fetch_earliest_snapshot_at(item_id="item-1", user_id="u1"))

    def test_insert_snapshots_posts_all_rows(self) -> None:
        posted: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            posted.extend(json.loads(request.content.decode()))
            return httpx.Response(201)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reader = PortfolioItemReader(
            supabase_url="https://example.supabase.co", service_role_key="service-role", client=client
        )

        inserted = reader.insert_snapshots(
            [{"portfolio_item_id": "1"}, {"portfolio_item_id": "2"}]
        )

        self.assertEqual(inserted, 2)
        self.assertEqual(len(posted), 2)

    def test_insert_snapshots_returns_zero_for_an_empty_list_without_a_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make a request for an empty list")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reader = PortfolioItemReader(
            supabase_url="https://example.supabase.co", service_role_key="service-role", client=client
        )

        self.assertEqual(reader.insert_snapshots([]), 0)

    def test_insert_snapshots_logs_and_returns_zero_on_http_error(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        reader = PortfolioItemReader(
            supabase_url="https://example.supabase.co", service_role_key="service-role", client=client
        )

        inserted = reader.insert_snapshots([{"portfolio_item_id": "1"}])

        self.assertEqual(inserted, 0)


class _StubCatalog:
    def __init__(self, response: CatalogSearchResponse) -> None:
        self._response = response

    def search(self, query, limit=5):
        return self._response


class _ExplodingCatalog:
    def search(self, query, limit=5):
        from app.services.pricing.catalog_search_service import CatalogSearchError

        raise CatalogSearchError("boom")


class _StubDetailCatalog:
    def __init__(self, *, history: list[CatalogHistoryPoint]) -> None:
        self._history = history

    def detail(self, catalog_id, history_limit=30):
        result = _result(id=catalog_id, confidence=0.96)
        return CatalogDetailResponse(result=result, history=self._history)


class _ExplodingDetailCatalog:
    def detail(self, catalog_id, history_limit=30):
        raise CatalogSearchError("boom")


class _RecordingHistoryReader:
    def __init__(self, *, earliest_existing: str | None) -> None:
        self._earliest_existing = earliest_existing
        self.inserted_rows: list[dict] = []

    def fetch_earliest_snapshot_at(self, *, item_id, user_id):
        return self._earliest_existing

    def insert_snapshots(self, rows):
        self.inserted_rows.extend(rows)
        return len(rows)


if __name__ == "__main__":
    unittest.main()
