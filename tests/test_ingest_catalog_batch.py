"""The ingest stage: claim one batch, write it, and only then stamp.

The property worth breaking a build over is the ORDER. A set stamped
tier3_refreshed_at from data that never landed is indistinguishable from a
real refresh until someone reads the prices -- and the old one-pass design
did exactly that on every partial failure. Several tests below do nothing but
prove the stamp cannot happen before or without a clean write.

The second property is that a failed ingest stays retryable FROM STORAGE.
Under the old design a write failure meant re-downloading identical bytes,
spending a second vendor slot out of 144/day, and completed-categories failed
9-16 batches per run -- so this is a real cost, not a hypothetical one.
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.catalog_batches import (
    INGEST_FAILED,
    INGESTED,
    INGESTING,
    VALIDATED,
    WRITE_REST,
)
from scripts.ingest_catalog_batch import main, parse_args, reap_stale_ingest_leases

CSV = "id,console-name,product-name,loose-price\n" + "".join(
    f"{i},Baseball Cards 1962 Bazooka,Card {i},$1.00\n" for i in range(1, 26)
)

BATCH = {
    "batch_id": "batch-1",
    "source": "sportscardspro",
    "status": VALIDATED,
    "storage_key": "sportscardspro/2026/09/08/batch-1.csv",
    "registry_ids": [f"r{i}" for i in range(350)],
    "console_uids": [f"G{i}" for i in range(350)],
    "requested_count": 350,
    "row_count": 25,
    "attempts": 1,
}


_UNSET = object()


class _Store:
    def __init__(self, *, batch=_UNSET, batches=None):
        self.base = "https://x.test"
        self.key = "k"
        # Sentinel, not None: an explicit batch=None means "empty queue",
        # which is a case worth testing and must not fall back to the default.
        self._batch = dict(BATCH) if batch is _UNSET else batch
        self._batches = batches or []
        self.updates: list[tuple[str, dict]] = []
        self.stamped: list[list[str]] = []
        self.claims = 0

    def batches(self, *, source, statuses):
        return [b for b in self._batches if b["status"] in statuses]

    def claim_validated_batch(self, *, source, claimed_by):
        self.claims += 1
        return dict(self._batch) if self._batch else None

    def download_object(self, key, destination):
        Path(destination).write_text(CSV)
        return len(CSV)

    def update(self, batch_id, patch):
        self.updates.append((batch_id, patch))

    def stamp_registry_refreshed(self, registry_ids):
        self.stamped.append(list(registry_ids))

    def statuses(self):
        return [p.get("status") for _, p in self.updates if "status" in p]


class _Stats:
    def __init__(self, written=20, skipped=5, failed=0, abandoned=0):
        self.catalog_write_stats = {
            "written": written, "skippedUnchanged": skipped, "failed": failed}
        self.timeout_retry_stats = {
            "timeouts": 1, "retries": 1, "rowsRecovered": 3, "rowsAbandoned": abandoned}


def _run(store, *, wrote=True, stats=None, argv=("--commit",)):
    stats = stats or _Stats()
    with patch("scripts.ingest_catalog_batch.BatchStore", return_value=store), \
         patch("scripts.ingest_catalog_batch.SupabaseCatalogClient", return_value=stats), \
         patch("scripts.ingest_catalog_batch.write_catalog_rows_with_retry",
               return_value=(wrote, 0)) as writer, \
         patch.dict("os.environ", {"SUPABASE_URL": "https://x.test",
                                   "SUPABASE_SERVICE_ROLE_KEY": "k"}):
        code = main(list(argv))
    return code, writer


class StampingOrderTest(unittest.TestCase):
    """The whole point of splitting ingest from download."""

    def test_a_clean_ingest_stamps_exactly_this_batch(self) -> None:
        store = _Store()
        _run(store)
        self.assertEqual(len(store.stamped), 1)
        self.assertEqual(store.stamped[0], BATCH["registry_ids"])
        self.assertEqual(len(store.stamped[0]), 350)

    def test_a_failed_write_stamps_nothing(self) -> None:
        store = _Store()
        _run(store, wrote=False)
        self.assertEqual(store.stamped, [], "registry stamped despite a failed write")
        self.assertIn(INGEST_FAILED, store.statuses())
        self.assertNotIn(INGESTED, store.statuses())

    def test_the_batch_is_only_marked_ingested_after_a_clean_write(self) -> None:
        store = _Store()
        _run(store)
        self.assertEqual(store.statuses()[-1], INGESTED)

    def test_no_other_registry_rows_are_touched(self) -> None:
        """Stamping is by explicit id list, never by a predicate."""
        store = _Store()
        _run(store)
        self.assertEqual(set(store.stamped[0]), set(BATCH["registry_ids"]))


class FailureIsRetryableFromStorageTest(unittest.TestCase):
    def test_a_failed_ingest_keeps_its_storage_key(self) -> None:
        """Retry must not need another vendor CSV slot."""
        store = _Store()
        _run(store, wrote=False)
        failed = [p for _, p in store.updates if p.get("status") == INGEST_FAILED][0]
        self.assertNotIn("storage_key", failed,
                         "the storage key was cleared, forcing a re-download")

    def test_a_failed_ingest_releases_the_lease(self) -> None:
        store = _Store()
        _run(store, wrote=False)
        failed = [p for _, p in store.updates if p.get("status") == INGEST_FAILED][0]
        self.assertIsNone(failed["claimed_at"])
        self.assertEqual(failed["last_error_class"], "write")

    def test_counters_are_recorded_even_on_failure(self) -> None:
        """A failed batch is where the numbers matter most."""
        store = _Store()
        _run(store, wrote=False, stats=_Stats(written=7, skipped=1, failed=3))
        failed = [p for _, p in store.updates if p.get("status") == INGEST_FAILED][0]
        self.assertEqual(failed["rows_written"], 7)
        self.assertEqual(failed["rows_failed"], 3)
        self.assertEqual(failed["write_path"], WRITE_REST)


class CountersTest(unittest.TestCase):
    def test_the_batch_row_records_the_write_outcome(self) -> None:
        store = _Store()
        _run(store, stats=_Stats(written=20, skipped=5, failed=0))
        final = [p for _, p in store.updates if p.get("status") == INGESTED][0]
        self.assertEqual(final["rows_written"], 20)
        self.assertEqual(final["rows_skipped"], 5)
        self.assertEqual(final["rows_failed"], 0)
        self.assertEqual(final["write_path"], WRITE_REST)
        self.assertIsInstance(final["ingest_ms"], int)

    def test_the_timeout_counters_reach_the_batch_row(self) -> None:
        """#205's counters at batch granularity: a run summary is lost if the
        process dies mid-write, the batch row is not."""
        store = _Store()
        _run(store, stats=_Stats(abandoned=4))
        final = [p for _, p in store.updates if p.get("status") == INGESTED][0]
        self.assertEqual(final["statement_timeouts"], 1)
        self.assertEqual(final["rows_recovered"], 3)
        self.assertEqual(final["rows_abandoned"], 4)


class DefaultSafeTest(unittest.TestCase):
    def test_without_commit_nothing_is_claimed_or_written(self) -> None:
        store = _Store(batches=[dict(BATCH)])
        _run(store, argv=())
        self.assertEqual(store.claims, 0)
        self.assertEqual(store.updates, [])
        self.assertEqual(store.stamped, [])

    def test_commit_is_off_by_default(self) -> None:
        self.assertFalse(parse_args([]).commit)


class NoVendorFetchTest(unittest.TestCase):
    """The ingester's input is a CSV already paid for."""

    def test_the_script_never_references_the_vendor_endpoint(self) -> None:
        source = Path("scripts/ingest_catalog_batch.py").read_text()
        for forbidden in ("download-custom", "csv_base_url", "SharedRateLimiter",
                          "pricecharting.com"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, source)


class EmptyQueueTest(unittest.TestCase):
    def test_no_validated_batch_is_a_quiet_no_op(self) -> None:
        store = _Store(batch=None)
        code, writer = _run(store)
        self.assertEqual(code, 0)
        self.assertEqual(store.updates, [])
        writer.assert_not_called()


class ReaperTest(unittest.TestCase):
    def test_a_stale_ingest_lease_returns_to_validated_not_pending(self) -> None:
        """The object is still in storage; pending would spend a vendor slot
        re-downloading what we already hold."""
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        store = _Store(batches=[{"batch_id": "stuck", "status": INGESTING, "claimed_at": old}])
        self.assertEqual(reap_stale_ingest_leases(store, source="sportscardspro", commit=True), 1)
        self.assertEqual(store.updates[0][1]["status"], VALIDATED)

    def test_a_fresh_ingest_lease_is_left_alone(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        store = _Store(batches=[{"batch_id": "busy", "status": INGESTING, "claimed_at": recent}])
        self.assertEqual(reap_stale_ingest_leases(store, source="sportscardspro", commit=True), 0)
        self.assertEqual(store.updates, [])


class ClaimIsCompareAndSwapTest(unittest.TestCase):
    """PostgREST cannot express FOR UPDATE SKIP LOCKED.

    A read-then-unconditional-write would let two ingesters take the same
    batch, double-writing the catalog and double-stamping the registry. The
    claim filters on status=validated so a lost race updates zero rows.
    """

    def test_the_claim_patch_filters_on_the_expected_status(self) -> None:
        source = Path("scripts/catalog_batch_store.py").read_text()
        claim = source[source.index("def claim_validated_batch"):]
        self.assertIn('"status": f"eq.{VALIDATED}"', claim)
        self.assertIn("return=representation", claim)

    def test_a_lost_race_returns_none_rather_than_a_stale_batch(self) -> None:
        source = Path("scripts/catalog_batch_store.py").read_text()
        claim = source[source.index("def claim_validated_batch"):]
        self.assertIn("if claimed:", claim)
        self.assertIn("return None", claim)


if __name__ == "__main__":
    unittest.main()
