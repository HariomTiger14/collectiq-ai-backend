"""Staging the bulk CSVs so a failed import does not cost a vendor slot.

Today the file is downloaded to a temp path, imported, and deleted in a
`finally`. If the import dies after the download -- a statement timeout, a
crash, a deploy mid-run -- the file is gone and the only retry is another
vendor download. That endpoint allows one request per ten minutes ACCOUNT-WIDE,
shared with the tier-3 rotation and the sets backfill, so a wasted download
takes a slot from another job rather than merely costing time.

Opt-in via --use-storage. Everything here must be inert without it, because the
cron keeps running the old path until that flag is added to its start command.
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from typing import Any

from scripts.catalog_batches import INGEST_FAILED, INGESTED, INGESTING, VALIDATED
from scripts.csv_refresh_storage import (
    mark_ingest_failed,
    mark_ingested,
    mark_ingesting,
    reap_stale_ingesting,
    resumable_batches,
    staged_today,
    stage_csv,
)
from scripts.refresh_pricecharting_catalog import parse_args


class _Store:
    """Records what a real BatchStore would have been asked to do."""

    def __init__(self, existing: list[dict[str, Any]] | None = None):
        self.uploads: list[tuple[str, pathlib.Path]] = []
        self.inserted: list[dict[str, Any]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[str] = []
        self._existing = existing or []

    def upload(self, key, path):
        self.uploads.append((key, pathlib.Path(path)))

    def insert(self, row):
        self.inserted.append(row)
        return dict(row)

    def update(self, batch_id, patch):
        self.updates.append((batch_id, patch))

    def batches(self, *, source, statuses):
        return [b for b in self._existing
                if b["source"] == source and b["status"] in statuses]

    def download_object(self, key, destination):
        self.downloads.append(key)
        pathlib.Path(destination).write_text(
            "id,product-name,console-name,loose-price\n1,Pikachu,Pokemon Cards,1200\n")
        return 1


def _csv() -> pathlib.Path:
    handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv")
    with handle:
        handle.write("id,product-name,console-name,loose-price\n"
                     "1,Pikachu,Pokemon Cards,1200\n")
    return pathlib.Path(handle.name)


class ItIsOffUnlessAskedForTest(unittest.TestCase):
    """The cron keeps its current behaviour until the flag is on the command."""

    def test_the_flag_defaults_to_off(self) -> None:
        self.assertFalse(parse_args([]).use_storage)

    def test_the_flag_can_be_turned_on(self) -> None:
        self.assertTrue(parse_args(["--use-storage"]).use_storage)


class TheCronRunsTheStagedPathTest(unittest.TestCase):
    """Inverted 2026-09-12 when the flag was switched on.

    This test previously asserted the cron did NOT pass --use-storage, so that
    enabling staging had to be a deliberate act rather than a diff nobody
    noticed. It now asserts the opposite, for the same reason in the other
    direction: turning it back off should be a decision too, not a quiet
    revert.

    It is kept rather than deleted because the yaml line is the one that
    matters. The argparse default has never been what production runs.
    """

    def _start_command(self) -> str:
        import re

        text = pathlib.Path("render.yaml").read_text()
        match = re.search(
            r"startCommand: python -m scripts\.refresh_pricecharting_catalog[^\n]*", text)
        self.assertIsNotNone(match, "refresh_pricecharting_catalog startCommand not found")
        return match.group(0)

    def test_the_cron_passes_use_storage(self) -> None:
        self.assertIn(
            "--use-storage", self._start_command(),
            "staging was switched off; that is an operational decision and "
            "should not happen by a silent revert of this line")

    def test_the_cron_still_passes_the_safe_batch_size(self) -> None:
        """Unrelated to staging, and the reason five nights wrote nothing."""
        self.assertIn("--batch-size 900", self._start_command())


class TheObjectLandsBeforeTheRowTest(unittest.TestCase):
    """Order matters, and only one way round is safe.

    A row pointing at an object that does not exist is worse than an object
    with no row: the orphan object is visible and disposable, while the
    dangling row gets claimed by the next run and fails on a 404 it cannot fix.
    """

    def test_upload_happens_before_the_row_is_written(self) -> None:
        store = _Store()
        path = _csv()
        self.addCleanup(path.unlink, missing_ok=True)

        order: list[str] = []
        store.upload = lambda k, p: order.append("upload")
        original_insert = store.insert
        store.insert = lambda row: (order.append("insert"), original_insert(row))[1]

        stage_csv(store, source_name="pokemon.csv", path=path)
        self.assertEqual(order, ["upload", "insert"])

    def test_the_row_records_where_the_object_is(self) -> None:
        store = _Store()
        path = _csv()
        self.addCleanup(path.unlink, missing_ok=True)
        row = stage_csv(store, source_name="pokemon.csv", path=path)
        self.assertEqual(row["storage_key"], store.uploads[0][0])
        self.assertIn("pokemon.csv/", row["storage_key"])
        self.assertTrue(row["storage_key"].endswith(f"{row['batch_id']}.csv"))

    def test_it_is_ready_to_ingest_immediately(self) -> None:
        """No validation gate: a bulk CSV is the whole category by definition,
        so there is nothing to compare it against."""
        store = _Store()
        path = _csv()
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertEqual(stage_csv(store, source_name="pokemon.csv",
                                   path=path)["status"], VALIDATED)

    def test_the_batch_is_tagged_with_the_file_name(self) -> None:
        """Keeps CSV rows distinguishable from the sports rows sharing the
        table, and matches the source_file the rows themselves carry."""
        store = _Store()
        path = _csv()
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertEqual(stage_csv(store, source_name="pokemon.csv",
                                   path=path)["source"], "pokemon.csv")


class AFileLeftBehindIsResumedTest(unittest.TestCase):
    """The property the whole change exists for."""

    def _batch(self, status, key="pokemon.csv/2026/09/12/abc.csv"):
        return {"batch_id": "abc", "source": "pokemon.csv",
                "status": status, "storage_key": key, "attempts": 0}

    def test_a_validated_batch_is_resumable(self) -> None:
        store = _Store([self._batch(VALIDATED)])
        self.assertEqual(len(resumable_batches(store, source_name="pokemon.csv")), 1)

    def test_a_failed_ingest_is_resumable(self) -> None:
        """Its object is still in storage, so the retry costs no vendor slot."""
        store = _Store([self._batch(INGEST_FAILED)])
        self.assertEqual(len(resumable_batches(store, source_name="pokemon.csv")), 1)

    def test_an_already_ingested_batch_is_not(self) -> None:
        store = _Store([self._batch(INGESTED)])
        self.assertEqual(resumable_batches(store, source_name="pokemon.csv"), [])

    def test_a_row_with_no_object_is_not_resumable(self) -> None:
        """A dangling row would send the next run at a 404 it cannot fix."""
        store = _Store([self._batch(VALIDATED, key=None)])
        self.assertEqual(resumable_batches(store, source_name="pokemon.csv"), [])

    def test_another_source_is_not_picked_up(self) -> None:
        store = _Store([{**self._batch(VALIDATED), "source": "magic.csv"}])
        self.assertEqual(resumable_batches(store, source_name="pokemon.csv"), [])

    def test_the_sports_batches_are_not_picked_up(self) -> None:
        """They share the table and have their own pipeline."""
        store = _Store([{**self._batch(VALIDATED), "source": "sportscardspro"}])
        self.assertEqual(resumable_batches(store, source_name="pokemon.csv"), [])


class TheStatusTellsTheTruthTest(unittest.TestCase):
    def _batch(self):
        return {"batch_id": "abc", "source": "pokemon.csv",
                "status": VALIDATED, "storage_key": "k", "attempts": 2}

    def test_claiming_counts_the_attempt(self) -> None:
        """Otherwise a file that fails forever is indistinguishable from a
        file on its first try."""
        store = _Store()
        mark_ingesting(store, self._batch(), claimed_by="refresh:pokemon.csv")
        _, patch = store.updates[0]
        self.assertEqual(patch["status"], INGESTING)
        self.assertEqual(patch["attempts"], 3)

    def test_success_clears_the_claim(self) -> None:
        store = _Store()
        mark_ingested(store, self._batch(), stats={"rows_written": 5})
        _, patch = store.updates[0]
        self.assertEqual(patch["status"], INGESTED)
        self.assertIsNone(patch["claimed_by"])
        self.assertIsNone(patch["last_error"])
        self.assertEqual(patch["rows_written"], 5)

    def test_failure_keeps_the_object_and_records_why(self) -> None:
        store = _Store()
        mark_ingest_failed(store, self._batch(), error="boom", error_class="write")
        _, patch = store.updates[0]
        self.assertEqual(patch["status"], INGEST_FAILED)
        self.assertEqual(patch["last_error"], "boom")
        self.assertNotIn("storage_key", patch,
                         "the object must stay -- that is what makes the retry free")

    def test_a_long_error_is_truncated_rather_than_rejected(self) -> None:
        store = _Store()
        mark_ingest_failed(store, self._batch(), error="x" * 9000, error_class="write")
        self.assertLessEqual(len(store.updates[0][1]["last_error"]), 2000)


if __name__ == "__main__":
    unittest.main()


class ACrashMidImportIsRecoverableTest(unittest.TestCase):
    """A process killed mid-import leaves the row INGESTING.

    That status is not resumable on its own, so without reaping, the next run
    downloads again -- spending the vendor slot this whole path exists to save.
    Exactly the shape of the orphaned 'running' ledger rows that have been
    sitting since 2026-09-03.
    """

    def _ingesting(self, claimed_at):
        return {"batch_id": "abc", "source": "pokemon.csv", "status": INGESTING,
                "storage_key": "k", "attempts": 1, "claimed_at": claimed_at}

    def test_the_claim_records_a_time_not_just_a_name(self) -> None:
        """A reaper needs a clock. claimed_by alone cannot tell an import
        running now from one abandoned last night."""
        store = _Store()
        mark_ingesting(store, {"batch_id": "abc", "attempts": 0},
                       claimed_by="refresh:pokemon.csv")
        _, patch = store.updates[0]
        self.assertIsNotNone(patch.get("claimed_at"))
        self.assertEqual(patch["claimed_by"], "refresh:pokemon.csv")

    def test_a_stale_lease_goes_back_to_validated(self) -> None:
        from datetime import datetime, timedelta, timezone

        stale = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        store = _Store([self._ingesting(stale)])
        self.assertEqual(reap_stale_ingesting(store, source_name="pokemon.csv"), 1)
        _, patch = store.updates[0]
        self.assertEqual(patch["status"], VALIDATED)
        self.assertIsNone(patch["claimed_at"])
        self.assertNotIn("storage_key", patch, "the object must survive reaping")

    def test_a_fresh_lease_is_left_alone(self) -> None:
        """Another process may genuinely be importing it right now."""
        from datetime import datetime, timezone

        store = _Store([self._ingesting(datetime.now(timezone.utc).isoformat())])
        self.assertEqual(reap_stale_ingesting(store, source_name="pokemon.csv"), 0)
        self.assertEqual(store.updates, [])

    def test_a_reaped_batch_is_then_resumable(self) -> None:
        """Reaping is only useful if it feeds the resume path."""
        store = _Store([{**self._ingesting(None), "status": VALIDATED}])
        self.assertEqual(len(resumable_batches(store, source_name="pokemon.csv")), 1)


class DrainingALeftoverDoesNotSkipTodayTest(unittest.TestCase):
    """Finishing yesterday's file is not the same as refreshing today.

    An earlier version returned straight after resuming, which would have
    quietly skipped a day's prices for any source whose previous night failed
    -- the failure hiding inside the fix for the failure.
    """

    def _batch(self, created_at):
        return {"batch_id": "abc", "source": "pokemon.csv", "status": VALIDATED,
                "storage_key": "k", "attempts": 0, "created_at": created_at}

    def test_a_file_staged_yesterday_does_not_count_as_today(self) -> None:
        from datetime import datetime, timedelta, timezone

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.assertFalse(staged_today(self._batch(yesterday)))

    def test_a_file_staged_today_does(self) -> None:
        from datetime import datetime, timezone

        self.assertTrue(
            staged_today(self._batch(datetime.now(timezone.utc).isoformat())))

    def test_a_row_with_no_timestamp_is_treated_as_not_today(self) -> None:
        """Fail towards downloading: a redundant fetch costs one slot, a
        skipped day costs a day of prices."""
        self.assertFalse(staged_today(self._batch(None)))

    def test_the_check_is_by_utc_date_not_elapsed_hours(self) -> None:
        """'Already refreshed today' is a calendar question -- the job runs
        once a day at a fixed hour, so 23 hours ago is still yesterday."""
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 9, 12, 14, 30, tzinfo=timezone.utc)
        self.assertTrue(staged_today(self._batch(
            (now - timedelta(hours=13)).isoformat()), now=now))
        self.assertFalse(staged_today(self._batch(
            (now - timedelta(hours=15)).isoformat()), now=now))


class MergingTwoImportsInOneRunTest(unittest.TestCase):
    """A source can now import twice in a run -- a leftover plus today."""

    def _merge(self, *summaries):
        from scripts.refresh_pricecharting_catalog import _merge_summaries

        return _merge_summaries(list(summaries))

    _OK = {"source": "pokemon.csv", "inputRows": 10, "validRows": 10,
           "importedRows": 3, "historyRows": 3, "failed": False,
           "failureReason": None}

    def test_a_single_import_is_returned_unchanged(self) -> None:
        self.assertEqual(self._merge(self._OK), self._OK)

    def test_counts_add_up(self) -> None:
        merged = self._merge(self._OK, self._OK)
        self.assertEqual(merged["validRows"], 20)
        self.assertEqual(merged["importedRows"], 6)
        self.assertEqual(merged["importsInRun"], 2)

    def test_yesterday_succeeding_does_not_mask_today_failing(self) -> None:
        merged = self._merge(self._OK, {**self._OK, "failed": True,
                                        "failureReason": "batch_write_failed"})
        self.assertTrue(merged["failed"])
        self.assertEqual(merged["failureReason"], "batch_write_failed")

    def test_today_succeeding_does_not_mask_yesterday_failing(self) -> None:
        merged = self._merge({**self._OK, "failed": True,
                              "failureReason": "batch_write_failed"}, self._OK)
        self.assertTrue(merged["failed"])


class RefreshSourceControlFlowTest(unittest.TestCase):
    """The loop itself, not just the helpers it calls.

    staged_today() and _merge_summaries() were both covered in isolation while
    the branch that uses them was not -- so a mutation making the resume return
    early, skipping the day's download entirely, passed every test. That is the
    bug this class exists to catch.
    """

    def _run(self, existing, *, created_at=None):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch

        import scripts.refresh_pricecharting_catalog as module

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        batches = []
        for status in existing:
            batches.append({
                "batch_id": f"b{len(batches)}", "source": "pokemon.csv",
                "status": status, "storage_key": "k", "attempts": 0,
                "created_at": created_at or yesterday,
            })
        store = _Store(batches)
        downloads: list[str] = []
        imports: list[str] = []

        def fake_download(**kwargs):
            downloads.append(kwargs["source"])
            path = _csv()
            self.addCleanup(path.unlink, missing_ok=True)
            return path

        def fake_import(**kwargs):
            imports.append(str(kwargs["path"]))
            return {"source": "pokemon.csv", "inputRows": 1, "validRows": 1,
                    "importedRows": 1, "historyRows": 1, "failed": False,
                    "failureReason": None}

        with patch.object(module, "download_source_to_temp_file", fake_download), \
             patch.object(module, "import_source_file", fake_import), \
             patch.object(module, "archive_source_file", lambda **kw: None):
            summary = module.refresh_source(
                source="pokemon", source_downloaded_at="2026-09-12T00:00:00Z",
                archive_dir="/tmp", batch_size=900, timeout_seconds=5,
                dry_run=False, client=object(), store=store)
        return summary, downloads, imports, store

    def test_a_leftover_from_yesterday_is_drained_AND_today_is_downloaded(self) -> None:
        summary, downloads, imports, _ = self._run([VALIDATED])
        self.assertEqual(len(imports), 2, "expected the leftover plus today's file")
        self.assertEqual(downloads, ["pokemon"],
                         "today's download was skipped; the day's prices are lost")
        self.assertEqual(summary["importsInRun"], 2)

    def test_a_file_already_staged_today_does_not_trigger_a_second_download(self) -> None:
        """It has already done today's work; fetching again spends a vendor
        slot to import the same file twice."""
        from datetime import datetime, timezone

        _, downloads, imports, _ = self._run(
            [VALIDATED], created_at=datetime.now(timezone.utc).isoformat())
        self.assertEqual(imports, imports[:1], "imported more than the staged file")
        self.assertEqual(downloads, [], "downloaded despite already having today's file")

    def test_no_leftover_downloads_exactly_once(self) -> None:
        summary, downloads, imports, _ = self._run([])
        self.assertEqual(downloads, ["pokemon"])
        self.assertEqual(len(imports), 1)
        self.assertNotIn("importsInRun", summary)

    def test_the_downloaded_file_is_staged_before_it_is_imported(self) -> None:
        _, _, _, store = self._run([])
        self.assertEqual(len(store.uploads), 1)
        self.assertEqual(len(store.inserted), 1)

    def test_a_stale_ingesting_row_is_reaped_and_then_resumed(self) -> None:
        """Without reaping this file is invisible and today re-downloads it."""
        from datetime import datetime, timedelta, timezone

        # Two different clocks, deliberately. claimed_at decides whether the
        # LEASE is stale; created_at decides whether the FILE is today's. An
        # earlier version of this test used one timestamp for both, so whether
        # it passed depended on where the wall clock sat relative to UTC
        # midnight -- it went green by luck and red four hours later.
        stale_lease = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        staged_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        from unittest.mock import patch

        import scripts.refresh_pricecharting_catalog as module

        batch = {"batch_id": "b0", "source": "pokemon.csv", "status": INGESTING,
                 "storage_key": "k", "attempts": 1, "claimed_at": stale_lease,
                 "created_at": staged_days_ago}
        store = _Store([batch])
        # The fake store returns rows by status, so reaping must flip it for
        # the later resume lookup to see it -- mirroring the real table.
        original_update = store.update

        def update(batch_id, patch_body):
            original_update(batch_id, patch_body)
            if "status" in patch_body:
                batch["status"] = patch_body["status"]

        store.update = update
        imports: list[str] = []
        with patch.object(module, "download_source_to_temp_file",
                          lambda **kw: _csv()), \
             patch.object(module, "import_source_file",
                          lambda **kw: (imports.append("x"), {
                              "source": "pokemon.csv", "inputRows": 1,
                              "validRows": 1, "importedRows": 1, "historyRows": 1,
                              "failed": False, "failureReason": None})[1]), \
             patch.object(module, "archive_source_file", lambda **kw: None):
            module.refresh_source(
                source="pokemon", source_downloaded_at="2026-09-12T00:00:00Z",
                archive_dir="/tmp", batch_size=900, timeout_seconds=5,
                dry_run=False, client=object(), store=store)
        self.assertEqual(store.updates[0][1]["status"], VALIDATED,
                         "the stale lease was not reaped")
        self.assertEqual(len(imports), 2, "reaped file was not resumed")
