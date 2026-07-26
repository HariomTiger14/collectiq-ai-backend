from pathlib import Path
import tempfile
import unittest

from scripts.refresh_pricecharting_catalog import _selected_sources, import_source_file


class RefreshPriceChartingCatalogTest(unittest.TestCase):
    def test_selected_sources_normalizes_and_validates_sources(self) -> None:
        self.assertEqual(
            _selected_sources("video-games, pokemon"),
            ["video_games", "pokemon"],
        )

    def test_selected_sources_rejects_unknown_source(self) -> None:
        with self.assertRaises(SystemExit):
            _selected_sources("watches")

    def test_import_source_file_dry_run_streams_and_counts_rows(self) -> None:
        path = _write_csv(
            "id,product-name,console-name,loose-price\n"
            "12345,Charizard,Pokemon Cards,79000\n"
        )
        self.addCleanup(path.unlink, missing_ok=True)

        summary = import_source_file(
            source_name="pokemon.csv",
            path=path,
            source_downloaded_at="2026-07-25T00:00:00Z",
            batch_size=1000,
            dry_run=True,
            client=None,
        )

        self.assertEqual(summary["source"], "pokemon.csv")
        self.assertEqual(summary["inputRows"], 1)
        self.assertEqual(summary["validRows"], 1)
        self.assertEqual(summary["importedRows"], 0)
        self.assertEqual(summary["historyRows"], 0)

    def test_import_source_file_upserts_in_batches_without_loading_all_rows(self) -> None:
        path = _write_csv(
            "id,product-name,console-name,loose-price\n"
            "1,Charizard,Pokemon Cards,79000\n"
            "2,Pikachu,Pokemon Cards,1200\n"
            "3,Blastoise,Pokemon Cards,3300\n"
        )
        self.addCleanup(path.unlink, missing_ok=True)
        client = _RecordingCatalogClient()

        summary = import_source_file(
            source_name="pokemon.csv",
            path=path,
            source_downloaded_at="2026-07-25T00:00:00Z",
            batch_size=2,
            dry_run=False,
            client=client,
        )

        self.assertEqual(summary["inputRows"], 3)
        self.assertEqual(summary["validRows"], 3)
        self.assertEqual(summary["importedRows"], 3)
        self.assertEqual(summary["historyRows"], 3)
        self.assertEqual([len(batch) for batch in client.batches], [2, 1])
        self.assertEqual([len(batch) for batch in client.history_batches], [2, 1])
        self.assertEqual(client.batches[0][0]["product_name"], "Charizard")


class _RecordingCatalogClient:
    def __init__(self) -> None:
        self.batches = []
        self.history_batches = []

    def sync_scd2_history_rows(self, rows, *, batch_size):
        self.history_batches.append(list(rows))
        return len(rows)

    def upsert_rows(self, rows, *, batch_size):
        self.batches.append(list(rows))
        return len(rows)


def _write_csv(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w")
    with handle:
        handle.write(text)
    return Path(handle.name)


if __name__ == "__main__":
    unittest.main()
