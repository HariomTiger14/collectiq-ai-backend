import unittest
from unittest.mock import patch

import httpx

from app.services.pricing.promote_scan_derived_catalog import (
    PricingCacheReader,
    ScanDerivedPromotionError,
    promote_scan_derived_rows,
    to_promoted_catalog_row,
)


class ToPromotedCatalogRowTest(unittest.TestCase):
    def test_maps_native_currency_price_not_aud_converted_value(self) -> None:
        # original_price/original_currency are the pre-FX-conversion, native
        # provider values; value_aud/low_estimate_aud/high_estimate_aud are
        # display-currency-converted and must NOT be promoted as if native.
        row = to_promoted_catalog_row(
            {
                "cache_key": "pricing:v3:AUD:abc123",
                "title": "Nike Air Force 1 '07",
                "category": "Sneakers",
                "pricing_provider": "kicksdb",
                "value_aud": 152.40,
                "original_price": 100.00,
                "original_currency": "USD",
                "normalized_identity": "sneakers nike air force 1 07",
                "checked_at": "2026-08-01T12:00:00Z",
                "evidence_json": {"sourceCount": 1},
            }
        )

        assert row is not None
        self.assertEqual(row["pricecharting_id"], "scan:pricing:v3:AUD:abc123")
        self.assertEqual(row["product_name"], "Nike Air Force 1 '07")
        self.assertEqual(row["category"], "Sneakers")
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["market_value_cents"], 10000)
        self.assertIsNone(row["low_estimate_cents"])
        self.assertIsNone(row["high_estimate_cents"])
        self.assertEqual(row["source_provider"], "kicksdb")
        self.assertEqual(row["source_kind"], "scan_derived")
        self.assertEqual(row["promoted_from_cache_key"], "pricing:v3:AUD:abc123")
        self.assertIn("content_hash", row)

    def test_returns_none_without_a_title(self) -> None:
        # pricecharting_catalog.product_name is NOT NULL — a cache row from
        # before the title column existed can't be promoted.
        row = to_promoted_catalog_row(
            {
                "cache_key": "pricing:v3:AUD:legacy",
                "category": "Sneakers",
                "original_price": 100.00,
                "original_currency": "USD",
            }
        )

        self.assertIsNone(row)

    def test_returns_none_without_a_cache_key(self) -> None:
        row = to_promoted_catalog_row({"title": "Charizard #4"})

        self.assertIsNone(row)

    def test_defaults_currency_and_category_when_missing(self) -> None:
        row = to_promoted_catalog_row(
            {
                "cache_key": "pricing:v3:AUD:xyz",
                "title": "Unknown Item",
            }
        )

        assert row is not None
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["category"], "Collectible")
        self.assertIsNone(row["market_value_cents"])

    def test_content_hash_changes_when_price_changes(self) -> None:
        base = {
            "cache_key": "pricing:v3:AUD:abc",
            "title": "Charizard #4",
            "category": "Cards",
            "original_price": 100.0,
            "original_currency": "USD",
        }
        higher_price = {**base, "original_price": 150.0}

        row_a = to_promoted_catalog_row(base)
        row_b = to_promoted_catalog_row(higher_price)

        assert row_a is not None and row_b is not None
        self.assertNotEqual(row_a["content_hash"], row_b["content_hash"])


class PricingCacheReaderTest(unittest.TestCase):
    def test_fetch_candidates_filters_by_hit_count_and_freshness(self) -> None:
        captured_params: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_params.update(dict(request.url.params))
            return httpx.Response(200, json=[{"cache_key": "a", "title": "Item A"}])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reader = PricingCacheReader(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=client,
        )

        candidates = reader.fetch_candidates(min_hit_count=1, limit=500)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(captured_params["hit_count"], "gte.1")
        self.assertEqual(captured_params["valuation_status"], "eq.market_estimated")
        self.assertEqual(captured_params["title"], "not.is.null")
        self.assertTrue(captured_params["expires_at"].startswith("gt."))

    def test_fetch_already_promoted_cache_keys(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {"promoted_from_cache_key": "pricing:v3:AUD:abc"},
                    {"promoted_from_cache_key": "pricing:v3:AUD:def"},
                ],
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reader = PricingCacheReader(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=client,
        )

        keys = reader.fetch_already_promoted_cache_keys()

        self.assertEqual(keys, {"pricing:v3:AUD:abc", "pricing:v3:AUD:def"})

    def test_raises_without_configuration(self) -> None:
        with self.assertRaises(ScanDerivedPromotionError):
            PricingCacheReader(supabase_url="", service_role_key="")


class PromoteScanDerivedRowsTest(unittest.TestCase):
    def test_dry_run_does_not_write_and_reports_skip_counts(self) -> None:
        candidates = [
            {"cache_key": "a", "title": "Charizard #4", "category": "Cards"},
            {"cache_key": "b", "category": "Sneakers"},  # no title -> skipped
            {"cache_key": "c", "title": "Already Promoted"},
        ]

        with patch(
            "app.services.pricing.promote_scan_derived_catalog.PricingCacheReader"
        ) as reader_cls, patch(
            "app.services.pricing.promote_scan_derived_catalog.SupabaseCatalogClient"
        ) as client_cls, patch(
            "app.services.pricing.promote_scan_derived_catalog.settings"
        ) as settings:
            settings.supabase_url = "https://example.supabase.co"
            settings.supabase_service_role_key = "service-role"
            reader = reader_cls.return_value
            reader.fetch_candidates.return_value = candidates
            reader.fetch_already_promoted_cache_keys.return_value = {"c"}

            result = promote_scan_derived_rows(min_hit_count=1, dry_run=True)

        self.assertEqual(result.candidateCount, 3)
        self.assertEqual(result.skippedAlreadyPromoted, 1)
        self.assertEqual(result.skippedMissingTitle, 1)
        self.assertEqual(result.promotedCount, 0)
        client_cls.assert_not_called()

    def test_live_run_upserts_only_valid_new_rows(self) -> None:
        candidates = [
            {"cache_key": "a", "title": "Charizard #4", "category": "Cards"},
        ]

        with patch(
            "app.services.pricing.promote_scan_derived_catalog.PricingCacheReader"
        ) as reader_cls, patch(
            "app.services.pricing.promote_scan_derived_catalog.SupabaseCatalogClient"
        ) as client_cls, patch(
            "app.services.pricing.promote_scan_derived_catalog.settings"
        ) as settings:
            settings.supabase_url = "https://example.supabase.co"
            settings.supabase_service_role_key = "service-role"
            reader = reader_cls.return_value
            reader.fetch_candidates.return_value = candidates
            reader.fetch_already_promoted_cache_keys.return_value = set()
            client_cls.return_value.upsert_rows.return_value = 1

            result = promote_scan_derived_rows(min_hit_count=1, dry_run=False)

        self.assertEqual(result.promotedCount, 1)
        client_cls.return_value.upsert_rows.assert_called_once()
        promoted_rows = client_cls.return_value.upsert_rows.call_args[0][0]
        self.assertEqual(promoted_rows[0]["pricecharting_id"], "scan:a")


if __name__ == "__main__":
    unittest.main()
