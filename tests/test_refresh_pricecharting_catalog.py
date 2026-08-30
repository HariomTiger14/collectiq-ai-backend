from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts.refresh_pricecharting_catalog import (
    _selected_sources,
    archive_source_file,
    download_source_to_temp_file,
    import_source_file,
)


class DownloadRetryTest(unittest.TestCase):
    # PriceCharting generates these CSVs on demand and intermittently
    # 503s (observed live 2026-08-29, where one 503 aborted the whole
    # daily refresh and every category went stale for a day).

    def _run(self, responses: list[object]) -> Path:
        # Counter lives on the instance so it is still readable when the
        # download raises rather than returning.
        self.calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            outcome = responses[min(self.calls, len(responses) - 1)]
            self.calls += 1
            if isinstance(outcome, int):
                return httpx.Response(outcome, text="")
            raise outcome
        transport = httpx.MockTransport(handler)
        real_client = httpx.Client

        def client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        with patch.dict("os.environ", {"PRICECHARTING_CSV_POKEMON_URL": "https://x/csv"}), \
             patch("scripts.refresh_pricecharting_catalog.time.sleep"), \
             patch("httpx.Client", client_factory):
            return download_source_to_temp_file(
                source="pokemon", source_name="pokemon.csv", timeout_seconds=5
            )

    def test_transient_503_is_retried_then_succeeds(self) -> None:
        path = self._run([503, 503, 200])
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertEqual(self.calls, 3)

    def test_client_error_is_not_retried(self) -> None:
        # A 401/404 is a configuration problem: retrying just delays the
        # failure by two minutes.
        with self.assertRaises(httpx.HTTPStatusError):
            self._run([404])
        self.assertEqual(self.calls, 1)


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

    def test_import_source_file_survives_a_failing_batch(self) -> None:
        path = _write_csv(
            "id,product-name,console-name,loose-price\n"
            "1,Charizard,Pokemon Cards,79000\n"
            "2,Pikachu,Pokemon Cards,1200\n"
            "3,Blastoise,Pokemon Cards,3300\n"
        )
        self.addCleanup(path.unlink, missing_ok=True)
        client = _FailFirstBatchCatalogClient()

        summary = import_source_file(
            source_name="pokemon.csv",
            path=path,
            source_downloaded_at="2026-07-25T00:00:00Z",
            batch_size=2,
            dry_run=False,
            client=client,
        )

        # First batch (2 rows) fails with a simulated statement timeout;
        # the run must continue and still import the second batch (1 row)
        # instead of raising and losing the whole source.
        self.assertEqual(summary["inputRows"], 3)
        self.assertEqual(summary["importedRows"], 1)
        self.assertEqual(summary["historyRows"], 1)
        self.assertEqual(len(client.upsert_calls), 2)

    def test_archive_source_file_copies_raw_csv_with_checksum(self) -> None:
        path = _write_csv(
            "id,product-name,console-name,loose-price\n"
            "1,Charizard,Pokemon Cards,79000\n"
        )
        self.addCleanup(path.unlink, missing_ok=True)
        with tempfile.TemporaryDirectory() as archive_dir:
            archive_path = archive_source_file(
                source_name="pokemon.csv",
                path=path,
                source_downloaded_at="2026-07-25T10:30:00Z",
                archive_dir=archive_dir,
                dry_run=False,
            )

            assert archive_path is not None
            self.assertEqual(archive_path.name, "pokemon.csv")
            self.assertEqual(archive_path.parent.name, "2026-07-25")
            self.assertEqual(archive_path.read_text(encoding="utf-8"), path.read_text())
            checksum = archive_path.with_suffix(".csv.sha256")
            self.assertTrue(checksum.exists())
            self.assertIn("pokemon.csv", checksum.read_text(encoding="utf-8"))

    def test_archive_source_file_dry_run_returns_destination_without_writing(self) -> None:
        path = _write_csv("id,product-name\n1,Charizard\n")
        self.addCleanup(path.unlink, missing_ok=True)
        with tempfile.TemporaryDirectory() as archive_dir:
            archive_path = archive_source_file(
                source_name="pokemon.csv",
                path=path,
                source_downloaded_at="2026-07-25T10:30:00Z",
                archive_dir=archive_dir,
                dry_run=True,
            )

            assert archive_path is not None
            self.assertEqual(archive_path.parent.name, "2026-07-25")
            self.assertFalse(archive_path.exists())


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


class _FailFirstBatchCatalogClient:
    def __init__(self) -> None:
        self.upsert_calls = []

    def sync_scd2_history_rows(self, rows, *, batch_size):
        return len(rows)

    def upsert_rows(self, rows, *, batch_size):
        self.upsert_calls.append(list(rows))
        if len(self.upsert_calls) == 1:
            raise SystemExit(
                "Supabase catalog import failed at rows 1-2 with HTTP 500: "
                '{"code":"57014","message":"canceling statement due to statement timeout"}'
            )
        return len(rows)


def _write_csv(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w")
    with handle:
        handle.write(text)
    return Path(handle.name)


if __name__ == "__main__":
    unittest.main()
