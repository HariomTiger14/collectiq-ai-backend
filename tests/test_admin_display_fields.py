"""Fields the admin console needs in order to render what is already stored.

Two bugs with the same shape: the backend held the data and never returned it,
so the portal could not display it and had no way to know it was missing.

  * The catalog admin note is written by the edit form and was never read
    back. The box rendered empty every time, so a saved note was invisible
    and the next edit silently replaced it -- destroying an audit trail
    entry with no indication that anything had been lost.

  * Valuation snapshots record which currency they were written in, but the
    two admin serializers dropped it. The console therefore subtracted an
    AUD point from a USD one and reported a ~34% crash on an item whose
    value had not changed (AUD 1.50 / 1.52 = USD 0.99 -- the same money,
    relabelled).

Additive only: no existing key changes, no value is computed or converted,
and `valueAud` keeps its (misleading) name so nothing downstream breaks. The
portal work that consumes these fields is a separate change.
"""

import unittest

from app.services.admin_catalog_service import _compact_catalog_row
from app.services.admin_portfolio_service import _compact_valuation_snapshot
from app.services.admin_user_service import (
    _compact_valuation_snapshot as _compact_user_valuation_snapshot,
)


def _pricecharting_row(**overrides) -> dict:
    row = {
        "pricecharting_id": "35593",
        "product_name": "Charizard",
        "category": "Pokemon Cards",
        "console_name": "Pokemon My First Battle",
        "upc": "1234",
        "loose_price_cents": 999,
        "currency": "USD",
        "updated_at": "2026-09-06T00:00:00Z",
        "admin_note": "corrected the title",
    }
    row.update(overrides)
    return row


def _kicksdb_row(**overrides) -> dict:
    row = {
        "kicksdb_id": "kd-1",
        "title": "Air Force 1",
        "sku": "AF1",
        "brand": "Nike",
        "category": "Sneaker",
        "min_price_cents": 12000,
        "currency": "USD",
        "updated_at": "2026-09-06T00:00:00Z",
        "image_url": "https://img.test/a.png",
    }
    row.update(overrides)
    return row


def _snapshot_row(**overrides) -> dict:
    row = {
        "id": "snap-1",
        "portfolio_item_id": "item-1",
        "value_aud": 1.5,
        "currency": "AUD",
        "display_string": "AUD $1.50",
        "valuation_status": "market_estimated",
        "valuation_strategy": "catalog_lookup",
        "priced_at": "2026-09-05T00:00:00Z",
    }
    row.update(overrides)
    return row


class CatalogAdminNoteTest(unittest.TestCase):
    def test_pricecharting_row_returns_the_saved_note(self) -> None:
        item = _compact_catalog_row(_pricecharting_row(), source="pricecharting")
        self.assertEqual(item["adminNote"], "corrected the title")

    def test_a_row_with_no_note_returns_none_rather_than_omitting_the_key(self) -> None:
        """A stable key lets the portal render an empty state without guessing."""
        item = _compact_catalog_row(
            _pricecharting_row(admin_note=None), source="pricecharting"
        )
        self.assertIn("adminNote", item)
        self.assertIsNone(item["adminNote"])

    def test_kicksdb_row_still_serializes_and_omits_the_note(self) -> None:
        """kicksdb_catalog has no admin_note column -- see §7 item 18g."""
        item = _compact_catalog_row(_kicksdb_row(), source="kicksdb")
        self.assertEqual(item["source"], "KicksDB")
        self.assertEqual(item["title"], "Air Force 1")
        self.assertNotIn("adminNote", item)

    def test_existing_catalog_keys_are_unchanged(self) -> None:
        item = _compact_catalog_row(_pricecharting_row(), source="pricecharting")
        for key in ("id", "title", "identifier", "category", "setName",
                    "source", "lastUpdated", "imageUrl", "pricing"):
            with self.subTest(key=key):
                self.assertIn(key, item)
        self.assertEqual(item["pricing"]["currency"], "USD")


class ValuationSnapshotCurrencyTest(unittest.TestCase):
    def test_portfolio_serializer_returns_currency(self) -> None:
        out = _compact_valuation_snapshot(_snapshot_row(currency="USD"))
        self.assertEqual(out["currency"], "USD")

    def test_user_serializer_returns_currency(self) -> None:
        out = _compact_user_valuation_snapshot(_snapshot_row(currency="USD"))
        self.assertEqual(out["currency"], "USD")

    def test_both_serializers_default_legacy_rows_to_aud(self) -> None:
        """Belt-and-braces: the column is NOT NULL DEFAULT 'AUD'."""
        row = _snapshot_row()
        row.pop("currency")
        for name, fn in (
            ("portfolio", _compact_valuation_snapshot),
            ("user", _compact_user_valuation_snapshot),
        ):
            with self.subTest(serializer=name):
                self.assertEqual(fn(row)["currency"], "AUD")

    def test_a_mixed_history_is_now_distinguishable(self) -> None:
        """The whole point: the console can now tell these two apart.

        Same money -- AUD 1.50 at the retired 1.52 rate is USD 0.99 -- which
        the console reported as a 34% crash because both arrived as bare
        numbers.
        """
        history = [
            _compact_valuation_snapshot(_snapshot_row(value_aud=1.5, currency="AUD")),
            _compact_valuation_snapshot(_snapshot_row(value_aud=0.99, currency="USD")),
        ]
        self.assertEqual({row["currency"] for row in history}, {"AUD", "USD"})

    def test_value_and_display_string_are_untouched(self) -> None:
        """Additive only -- no renaming, no computing, no converting."""
        row = _snapshot_row()
        out = _compact_valuation_snapshot(row)
        self.assertEqual(out["valueAud"], 1.5)
        self.assertEqual(out["displayString"], "AUD $1.50")
        self.assertEqual(out["valuationStrategy"], "catalog_lookup")
        self.assertEqual(out["pricedAt"], "2026-09-05T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
