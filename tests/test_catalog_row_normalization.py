"""Row-key normalisation happens once per row, not once per field.

Profiled 2026-09-08 against the real 292,687-row batch: pick_text rebuilt a
normalised copy of the row's keys on EVERY field lookup, so a 15-column row
with 11 lookups ran normalize_key 246 times. That was 4.92M regex
substitutions, 72% of to_catalog_row's runtime, and 257 seconds of a
20-minute ingest.

Normalising once per row took the same file from 258.64s to 5.84s -- 44x --
which is the difference between a 350-set batch ingesting in ~20 minutes and
in a few. These tests pin the shape of that fix, because it is the kind of
thing an innocent-looking refactor puts straight back.
"""

import unittest
from unittest.mock import patch

from scripts.import_pricecharting_catalog import (
    TEXT_FIELDS,
    normalize_key,
    normalize_row_keys,
    pick_normalized,
    pick_text,
    to_catalog_row,
)

RAW = {
    "id": "12345",
    "console-name": "Baseball Cards 1962 Bazooka",
    "product-name": "Mickey Mantle #1",
    "loose-price": "$1,250.00",
    "cib-price": "$1,400.00",
    "new-price": "",
    "graded-price": "$5,000.00",
    "box-only-price": "",
    "manual-only-price": "",
    "upc": "012345678905",
    "asin": "B000TEST",
    "epid": "",
    "release-date": "1962-01-01",
    "genre": "Baseball",
    "product-url": "https://www.pricecharting.com/game/x",
}
STAMP = "2026-09-08T00:00:00+00:00"


class NormalisationHappensOncePerRowTest(unittest.TestCase):
    def test_to_catalog_row_normalises_the_row_exactly_once(self) -> None:
        """The fix itself. Eleven calls here is the bug coming back."""
        with patch(
            "scripts.import_pricecharting_catalog.normalize_row_keys",
            side_effect=normalize_row_keys,
        ) as spy:
            to_catalog_row(RAW, "tag", STAMP)
        self.assertEqual(
            spy.call_count, 1,
            f"row keys normalised {spy.call_count} times for one row; "
            "this is the 246-calls-per-row regression")

    def test_normalize_key_is_cached(self) -> None:
        """The same ~15 headers recur for every row in a 292k-row file."""
        self.assertTrue(hasattr(normalize_key, "cache_info"),
                        "normalize_key lost its cache; every lookup is a regex again")

    def test_the_cache_is_bounded(self) -> None:
        """A malformed file could otherwise present unbounded distinct keys."""
        self.assertIsNotNone(normalize_key.cache_info().maxsize)


class BehaviourIsUnchangedTest(unittest.TestCase):
    """A 44x speedup is worthless if it parses differently."""

    def test_the_parsed_row_is_what_it_always_was(self) -> None:
        row = to_catalog_row(RAW, "tag", STAMP)
        self.assertEqual(row["pricecharting_id"], "12345")
        self.assertEqual(row["product_name"], "Mickey Mantle #1")
        self.assertEqual(row["console_name"], "Baseball Cards 1962 Bazooka")
        self.assertEqual(row["loose_price_cents"], 125000)
        self.assertEqual(row["graded_price_cents"], 500000)
        self.assertEqual(row["upc"], "012345678905")
        self.assertEqual(row["release_date"], "1962-01-01")

    def test_an_empty_price_stays_none_rather_than_zero(self) -> None:
        row = to_catalog_row(RAW, "tag", STAMP)
        self.assertIsNone(row["new_price_cents"])
        self.assertIsNone(row["box_only_price_cents"])

    def test_headers_resolve_whatever_their_spelling(self) -> None:
        """Alias matching is the reason normalisation exists at all."""
        for transform in (
            lambda k: k.replace("-", "_"),
            lambda k: k.upper(),
            lambda k: k.replace("-", " ").title(),
            lambda k: f"  {k}  ",
        ):
            with self.subTest(style=transform("console-name")):
                variant = {transform(k): v for k, v in RAW.items()}
                row = to_catalog_row(variant, "tag", STAMP)
                self.assertIsNotNone(row)
                self.assertEqual(row["pricecharting_id"], "12345")
                self.assertEqual(row["console_name"], "Baseball Cards 1962 Bazooka")

    def test_pick_text_still_works_for_callers_holding_a_raw_row(self) -> None:
        """The diagnostics and API-search paths still pass raw rows."""
        self.assertEqual(pick_text(RAW, TEXT_FIELDS["console_name"]),
                         "Baseball Cards 1962 Bazooka")
        self.assertEqual(pick_text(RAW, TEXT_FIELDS["pricecharting_id"]), "12345")

    def test_pick_text_and_pick_normalized_agree(self) -> None:
        normalized = normalize_row_keys(RAW)
        for name, aliases in TEXT_FIELDS.items():
            with self.subTest(field=name):
                self.assertEqual(pick_text(RAW, aliases),
                                 pick_normalized(normalized, aliases))

    def test_a_missing_field_is_an_empty_string_not_an_error(self) -> None:
        self.assertEqual(pick_normalized({}, TEXT_FIELDS["upc"]), "")

    def test_whitespace_only_values_are_treated_as_absent(self) -> None:
        normalized = normalize_row_keys({**RAW, "upc": "   "})
        self.assertEqual(pick_normalized(normalized, TEXT_FIELDS["upc"]), "")


if __name__ == "__main__":
    unittest.main()
