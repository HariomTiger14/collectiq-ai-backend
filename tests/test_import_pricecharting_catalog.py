import unittest

from scripts.import_pricecharting_catalog import (
    normalized_identity,
    parse_price_cents,
    to_catalog_row,
)


class ImportPriceChartingCatalogTest(unittest.TestCase):
    def test_parse_price_cents_keeps_pricecharting_pennies(self) -> None:
        self.assertEqual(parse_price_cents("3325"), 3325)
        self.assertEqual(parse_price_cents("") , None)

    def test_parse_price_cents_converts_currency_strings(self) -> None:
        self.assertEqual(parse_price_cents("$33.25"), 3325)
        self.assertEqual(parse_price_cents("33.25"), 3325)
        self.assertEqual(parse_price_cents("1,234"), 1234)

    def test_to_catalog_row_maps_pricecharting_csv_fields(self) -> None:
        row = to_catalog_row(
            {
                "id": "12345",
                "product-name": "Mario Kart 8 Deluxe",
                "console-name": "Nintendo Switch",
                "loose-price": "3150",
                "cib-price": "3500",
                "new-price": "4009",
                "upc": "045496590475",
                "release-date": "2017-04-28",
            },
            source_file="price-guide.csv",
            source_downloaded_at="2026-07-25T00:00:00Z",
        )

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["pricecharting_id"], "12345")
        self.assertEqual(row["product_name"], "Mario Kart 8 Deluxe")
        self.assertEqual(row["console_name"], "Nintendo Switch")
        self.assertEqual(row["category"], "Nintendo Switch")
        self.assertEqual(row["loose_price_cents"], 3150)
        self.assertEqual(row["cib_price_cents"], 3500)
        self.assertEqual(row["new_price_cents"], 4009)
        self.assertEqual(row["release_date"], "2017-04-28")
        self.assertEqual(row["normalized_identity"], "mario kart 8 deluxe nintendo switch")

    def test_to_catalog_row_skips_rows_without_identity(self) -> None:
        self.assertIsNone(
            to_catalog_row(
                {"loose-price": "1234"},
                source_file="price-guide.csv",
                source_downloaded_at="2026-07-25T00:00:00Z",
            )
        )

    def test_normalized_identity_compacts_spacing(self) -> None:
        self.assertEqual(
            normalized_identity("  Mario   Kart 8 Deluxe ", " Nintendo Switch "),
            "mario kart 8 deluxe nintendo switch",
        )


if __name__ == "__main__":
    unittest.main()
