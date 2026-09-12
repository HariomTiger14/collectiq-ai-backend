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
    resumable_batches,
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


class TheCronIsStillOnTheOldPathTest(unittest.TestCase):
    """Staging ships as code first and is switched on separately.

    The switch is a deliberate operational step, taken only once a 14:30
    ledger looks quiet -- not something that rides along with the branch that
    wrote the feature. This fails the moment the start command starts staging,
    so turning it on has to be a decision rather than a diff nobody noticed.
    """

    def _start_command(self) -> str:
        import re

        text = pathlib.Path("render.yaml").read_text()
        match = re.search(
            r"startCommand: python -m scripts\.refresh_pricecharting_catalog[^\n]*", text)
        self.assertIsNotNone(match, "refresh_pricecharting_catalog startCommand not found")
        return match.group(0)

    def test_the_cron_does_not_pass_use_storage(self) -> None:
        self.assertNotIn(
            "--use-storage", self._start_command(),
            "the cron was switched to storage; that is an operational decision, "
            "not part of the branch that built it")

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
