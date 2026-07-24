import unittest
from unittest.mock import patch

from scripts.import_pricecharting_catalog import (
    download_env_sources,
    load_rows_from_text,
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

    def test_load_rows_from_text_parses_csv_download(self) -> None:
        rows = load_rows_from_text(
            "id,product-name,console-name,loose-price\n"
            "12345,Mario Kart 8 Deluxe,Nintendo Switch,3150\n"
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product-name"], "Mario Kart 8 Deluxe")

    def test_download_env_sources_uses_configured_category_urls(self) -> None:
        transport = _FakeTransport(
            {
                "https://pricecharting.test/video-games.csv": (
                    "id,product-name,console-name,loose-price\n"
                    "12345,Mario Kart 8 Deluxe,Nintendo Switch,3150\n"
                ),
                "https://pricecharting.test/pokemon.csv": (
                    "id,product-name,console-name,loose-price\n"
                    "999,Charizard,Pokemon Cards,120000\n"
                ),
            }
        )

        with patch.dict(
            "os.environ",
            {
                "PRICECHARTING_CSV_VIDEO_GAMES_URL": "https://pricecharting.test/video-games.csv",
                "PRICECHARTING_CSV_POKEMON_URL": "https://pricecharting.test/pokemon.csv",
                "PRICECHARTING_CSV_MAGIC_URL": "",
                "PRICECHARTING_CSV_YUGIOH_URL": "",
                "PRICECHARTING_CSV_ONE_PIECE_URL": "",
            },
            clear=False,
        ), patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport

            sources = download_env_sources(timeout_seconds=1)

        self.assertEqual([source.name for source in sources], ["video_games.csv", "pokemon.csv"])
        self.assertEqual(sources[0].rows[0]["product-name"], "Mario Kart 8 Deluxe")
        self.assertEqual(sources[1].rows[0]["product-name"], "Charizard")

    def test_download_env_sources_requires_at_least_one_url(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "PRICECHARTING_CSV_VIDEO_GAMES_URL": "",
                "PRICECHARTING_CSV_POKEMON_URL": "",
                "PRICECHARTING_CSV_MAGIC_URL": "",
                "PRICECHARTING_CSV_YUGIOH_URL": "",
                "PRICECHARTING_CSV_ONE_PIECE_URL": "",
            },
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                download_env_sources(timeout_seconds=1)

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


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _FakeTransport:
    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses

    def get(self, url: str, **kwargs):
        return _FakeResponse(self._responses[url])
