"""A batch that fails mid-write must not be recorded as refreshed.

The CSV path now streams to disk and writes in chunks, so a 300-set batch is
no longer one atomic in-memory operation. That makes the checkpoint rule load
bearing: if the container dies or a chunk write fails partway through, those
sets must stay at the front of the rotation rather than being stamped as
done. Otherwise a killed run silently skips whatever it had half-written,
and nothing ever refreshes it again.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.backfill_pricecharting_sets import CsvDownload
from scripts.refresh_sportscardspro_rotation import main, parse_args

CSV = "id,console-name,product-name,loose-price\n" + "".join(
    f"{i},Baseball Cards Set,Card {i},$1.00\n" for i in range(1, 51)
)


class _Registry:
    """Stands in for Tier3RegistryClient, recording what got stamped."""

    def __init__(self, rows):
        self._rows = rows
        self.refreshed: list[str] = []
        self.failures: list[str] = []

    def fetch_rotation_rows(self, *, limit):
        return self._rows[:limit]

    def mark_tier3_refreshed(self, ids):
        self.refreshed.extend(ids)

    def record_tier3_failures(self, ids, *, error):
        self.failures.extend(ids)


def _rows(n):
    return [
        {"registry_id": f"r{i}", "console_uid": f"G{i}", "set_name": f"Set {i}"}
        for i in range(n)
    ]


class Tier3FailureSafetyTest(unittest.TestCase):
    def _run(self, *, write_ok, argv=None):
        registry = _Registry(_rows(4))
        made: list[Path] = []

        def fake_fetch(*a, **kw):
            handle, name = tempfile.mkstemp(prefix="pricecharting-batch-", suffix=".csv")
            path = Path(name)
            with open(handle, "w", encoding="utf-8") as out:
                out.write(CSV)
            made.append(path)
            return CsvDownload(path, "utf-8")

        writes: list[int] = []

        def fake_write(client, rows, *, batch_size, attempts, backoff_seconds):
            writes.append(len(rows))
            return write_ok, 0

        argv = argv or ["--batch-size", "2", "--max-requests", "2", "--ingest-chunk-rows", "20"]
        mod = "scripts.refresh_sportscardspro_rotation"
        with mock.patch(f"{mod}.Tier3RegistryClient", return_value=registry), \
             mock.patch(f"{mod}.SupabaseCatalogClient") as catalog, \
             mock.patch(f"{mod}.fetch_batch_csv_file_with_retry", fake_fetch), \
             mock.patch(f"{mod}.write_catalog_rows_with_retry", fake_write), \
             mock.patch(f"{mod}.SharedRateLimiter") as limiter, \
             mock.patch.dict("os.environ", {"SUPABASE_URL": "https://x.test",
                                            "SUPABASE_SERVICE_ROLE_KEY": "k",
                                            "PRICECHARTING_API_TOKEN": "t"}):
            limiter.return_value.acquire.return_value = True
            # The run summary serialises these, so they have to be real.
            catalog.return_value.catalog_write_stats = {
                "written": 0, "skippedUnchanged": 0, "failed": 0
            }
            catalog.return_value.price_history_stats = {
                "attempted": 0, "inserted": 0, "duplicateSkipped": 0, "failed": 0
            }
            catalog.return_value.phase_seconds = {}
            main(argv)
        return registry, made, writes

    def test_a_failed_chunk_write_leaves_the_sets_unstamped(self):
        registry, _made, writes = self._run(write_ok=False)
        self.assertTrue(writes, "the write path should have been reached")
        self.assertEqual(
            registry.refreshed, [],
            "sets whose write failed must stay at the front of the rotation",
        )

    def test_a_successful_batch_is_stamped(self):
        registry, _made, _writes = self._run(write_ok=True)
        self.assertEqual(sorted(registry.refreshed), ["r0", "r1", "r2", "r3"])

    def test_rows_are_written_in_bounded_chunks_not_one_batch(self):
        """50 rows per set, 2 sets per batch, chunk size 20 -> the writer must
        see several bounded chunks rather than one 100-row list."""
        _registry, _made, writes = self._run(write_ok=True)
        self.assertTrue(writes)
        self.assertTrue(
            all(n <= 20 for n in writes), f"chunks exceeded the bound: {writes}"
        )

    def test_temp_files_are_removed_on_both_success_and_failure(self):
        for write_ok in (True, False):
            with self.subTest(write_ok=write_ok):
                _registry, made, _writes = self._run(write_ok=write_ok)
                self.assertTrue(made, "the fetch should have produced files")
                for path in made:
                    self.assertFalse(path.exists(), f"leaked {path}")


if __name__ == "__main__":
    unittest.main()
