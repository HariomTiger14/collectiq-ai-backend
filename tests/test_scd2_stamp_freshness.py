"""A long-running job must stamp each batch with its own download time.

source_downloaded_at becomes the new history version's valid_from AND the
valid_to used to close the previous current row. Computed once per run, it
goes stale as the run goes on -- and these jobs run for hours while tier-1
rewrites the same rows hourly. Closing a row that was rewritten at 05:40
with a valid_to of 04:45 gives valid_to < valid_from and Postgres rejects it
with 23514 against pricecharting_catalog_history_valid_window_check.

Observed in production 2026-09-05: 600 history rows lost in a single batch
of the completed-categories run. Tier-3 hit this on 2026-09-01 and was
fixed; these jobs were not.
"""

import unittest
from datetime import datetime, timezone
from unittest import mock

from scripts.backfill_pricecharting_sets import CsvDownload

CSV = "id,console-name,product-name,loose-price\n" + "".join(
    f"{i},Comic Books Set,Item {i},$1.00\n" for i in range(1, 6)
)


def _download(*_a, **_kw):
    import tempfile
    from pathlib import Path

    handle, name = tempfile.mkstemp(suffix=".csv")
    path = Path(name)
    with open(handle, "w", encoding="utf-8") as out:
        out.write(CSV)
    return CsvDownload(path, "utf-8")


class CategoriesStampTest(unittest.TestCase):
    def test_each_batch_is_stamped_with_its_own_time(self):
        mod = "scripts.refresh_completed_pricecharting_categories"
        rows = [
            {"registry_id": f"r{i}", "console_uid": f"G{i}",
             "set_name": f"Set {i}", "category": "comic-books"}
            for i in range(4)
        ]
        stamps: list[str] = []
        real_to_catalog_row = __import__(
            "scripts.import_pricecharting_catalog", fromlist=["to_catalog_row"]
        ).to_catalog_row

        def spy(raw, source_file, source_downloaded_at):
            stamps.append(source_downloaded_at)
            return real_to_catalog_row(raw, source_file, source_downloaded_at)

        # Two batches, and the clock advances a minute between them -- the
        # shape of a real run, where tier-1 writes in the gap.
        from datetime import timedelta

        clock = [datetime(2026, 9, 5, 4, 45, tzinfo=timezone.utc)]

        class _Clock:
            @staticmethod
            def now(tz=None):
                value = clock[0]
                clock[0] = value + timedelta(minutes=30)
                return value

        with mock.patch(f"{mod}.SupabaseRegistryReader") as reader, \
             mock.patch(f"{mod}.SupabaseCatalogClient") as catalog, \
             mock.patch(f"{mod}.fetch_batch_csv_file", _download), \
             mock.patch(f"{mod}.to_catalog_row", spy), \
             mock.patch(f"{mod}.write_catalog_rows", return_value=True), \
             mock.patch(f"{mod}.SharedRateLimiter") as limiter, \
             mock.patch(f"{mod}.datetime", _Clock), \
             mock.patch.dict("os.environ", {"SUPABASE_URL": "https://x.test",
                                            "SUPABASE_SERVICE_ROLE_KEY": "k",
                                            "PRICECHARTING_API_TOKEN": "t"}):
            reader.return_value.fetch_refreshable_rows.return_value = rows
            limiter.return_value.acquire.return_value = True
            catalog.return_value.catalog_write_stats = {
                "written": 0, "skippedUnchanged": 0, "failed": 0
            }
            catalog.return_value.price_history_stats = {
                "attempted": 0, "inserted": 0, "duplicateSkipped": 0, "failed": 0
            }
            from scripts.refresh_completed_pricecharting_categories import main

            main(["--batch-size", "2", "--catalog-batch-size", "40"])

        self.assertTrue(stamps, "no rows were converted")
        self.assertGreater(
            len(set(stamps)), 1,
            "every batch shared one timestamp -- that is the 23514 bug",
        )


class CatalogRefreshStampTest(unittest.TestCase):
    def test_the_stamp_is_taken_inside_the_source_loop(self):
        """Sources are 610s apart and this job averages 60 minutes, so a
        run-wide stamp is stale by the time the later ones are written."""
        import inspect

        from scripts import refresh_pricecharting_catalog as mod

        source = inspect.getsource(mod.main)
        loop_at = source.index("for source in selected_sources:")
        stamp_at = source.index("source_downloaded_at = datetime.now")
        self.assertGreater(
            stamp_at, loop_at,
            "source_downloaded_at is computed before the loop, so every "
            "source shares one stamp",
        )


if __name__ == "__main__":
    unittest.main()
