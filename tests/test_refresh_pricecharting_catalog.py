import unittest
from unittest.mock import patch

from scripts.refresh_pricecharting_catalog import _selected_sources, refresh_source


class RefreshPriceChartingCatalogTest(unittest.TestCase):
    def test_selected_sources_normalizes_and_validates_sources(self) -> None:
        self.assertEqual(
            _selected_sources("video-games, pokemon"),
            ["video_games", "pokemon"],
        )

    def test_selected_sources_rejects_unknown_source(self) -> None:
        with self.assertRaises(SystemExit):
            _selected_sources("watches")

    def test_refresh_source_dry_run_downloads_one_source_and_counts_rows(self) -> None:
        with patch(
            "scripts.refresh_pricecharting_catalog.download_env_sources",
            return_value=[
                _CatalogSource(
                    name="pokemon.csv",
                    rows=[
                        {
                            "id": "12345",
                            "product-name": "Charizard",
                            "console-name": "Pokemon Cards",
                            "loose-price": "79000",
                        }
                    ],
                )
            ],
        ):
            summary = refresh_source(
                source="pokemon",
                source_downloaded_at="2026-07-25T00:00:00Z",
                batch_size=1000,
                timeout_seconds=1,
                dry_run=True,
                client=None,
            )

        self.assertEqual(summary["source"], "pokemon.csv")
        self.assertEqual(summary["inputRows"], 1)
        self.assertEqual(summary["validRows"], 1)
        self.assertEqual(summary["importedRows"], 0)


class _CatalogSource:
    def __init__(self, *, name, rows) -> None:
        self.name = name
        self.rows = rows


if __name__ == "__main__":
    unittest.main()
