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

import httpx

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

    def claim_ingestable_batch(self, *, source, claimed_by, max_attempts):
        self.claims += 1
        self.claim_max_attempts = max_attempts
        if not self._batch:
            return None
        if int(self._batch.get("attempts") or 0) >= max_attempts:
            return None
        return dict(self._batch)

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
    """Stands in for SupabaseCatalogClient, and must carry every accumulator
    the real one does -- a fake that is missing an attribute the caller reads
    fails at runtime and nowhere else."""

    def __init__(self, written=20, skipped=5, failed=0, abandoned=0,
                 phase_seconds=None):
        self.catalog_write_stats = {
            "written": written, "skippedUnchanged": skipped, "failed": failed}
        self.timeout_retry_stats = {
            "timeouts": 1, "retries": 1, "rowsRecovered": 3, "rowsAbandoned": abandoned}
        self.phase_seconds = phase_seconds if phase_seconds is not None else {
            "unchanged_detection": 1.5, "catalog_upsert": 9.0,
            "scd2_comparison": 0.75, "price_snapshot_insert": 0.5,
            "scd2_close": 0.1, "scd2_insert": 0.25}
        self.price_history_stats = {
            "attempted": 12, "inserted": 11, "duplicateSkipped": 1, "failed": 0}


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
        claim = source[source.index("def claim_ingestable_batch"):]
        # Filters on the status the row was READ with, since a batch may be
        # claimed from validated or from ingest_failed.
        self.assertIn("f\"eq.{candidate['status']}\"", claim)
        self.assertIn("return=representation", claim)

    def test_a_lost_race_returns_none_rather_than_a_stale_batch(self) -> None:
        source = Path("scripts/catalog_batch_store.py").read_text()
        claim = source[source.index("def claim_ingestable_batch"):]
        self.assertIn("if claimed:", claim)
        self.assertIn("return None", claim)




class FailedBatchesAreRetriedFromStorageTest(unittest.TestCase):
    """The whole value of staging the file.

    Found live 2026-09-08: the ingester claimed only `validated`, but a
    failure sets `ingest_failed`, so a failed batch had to be reset by hand
    -- twice. Without this the storage design does not actually deliver
    "download once, retry safely".
    """

    def test_the_claim_covers_validated_and_ingest_failed(self) -> None:
        source = Path("scripts/catalog_batch_store.py").read_text()
        claim = source[source.index("def claim_ingestable_batch"):]
        self.assertIn("statuses=[VALIDATED, INGEST_FAILED]", claim)

    def test_validation_failed_is_never_retried(self) -> None:
        """A wrong-catalog file retried is just the wrong catalog, later.

        Asserted against the queried statuses, not the whole method -- the
        docstring names VALIDATION_FAILED precisely to explain the exclusion.
        """
        source = Path("scripts/catalog_batch_store.py").read_text()
        line = next(l for l in source.splitlines() if "statuses=[" in l and "self.batches" in l)
        self.assertIn("VALIDATED", line)
        self.assertIn("INGEST_FAILED", line)
        self.assertNotIn("VALIDATION_FAILED", line)

    def test_the_cas_filters_on_the_status_the_row_was_read_with(self) -> None:
        """Two possible source statuses, so the filter cannot be hardcoded."""
        source = Path("scripts/catalog_batch_store.py").read_text()
        claim = source[source.index("def claim_ingestable_batch"):]
        self.assertIn('f"eq.{candidate[\'status\']}"', claim)

    def test_a_batch_over_the_attempt_limit_is_not_claimed(self) -> None:
        """Exercised against the REAL store, not the fake.

        The first version of this test asserted the fake's own attempt check,
        so removing the limit from BatchStore changed nothing and the test
        still passed -- it was testing the test.
        """
        from scripts.catalog_batch_store import BatchStore

        patched = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[
                    {**BATCH, "batch_id": "spent", "attempts": 5},
                    {**BATCH, "batch_id": "fresh", "attempts": 1},
                ])
            patched.append(str(request.url))
            return httpx.Response(200, json=[{**BATCH, "batch_id": "fresh"}])

        transport = httpx.MockTransport(handler)
        real = httpx.Client
        store = BatchStore(supabase_url="https://x.test", service_role_key="k",
                           timeout_seconds=5)
        with patch("scripts.catalog_batch_store.httpx.Client",
                   side_effect=lambda *a, **kw: real(*a, transport=transport,
                                                     **{k: v for k, v in kw.items()})):
            claimed = store.claim_ingestable_batch(
                source="sportscardspro", claimed_by="test", max_attempts=5)
        self.assertIsNotNone(claimed)
        self.assertEqual(len(patched), 1, "more than one batch was claimed")
        self.assertIn("fresh", patched[0],
                      "claimed the batch that had already used its attempts")
        self.assertNotIn("spent", patched[0])

    def test_a_batch_under_the_attempt_limit_is_claimed(self) -> None:
        store = _Store(batch={**BATCH, "attempts": 4})
        _run(store)
        self.assertEqual(store.statuses()[-1], INGESTED)

    def test_the_attempt_limit_is_passed_to_the_store(self) -> None:
        store = _Store()
        _run(store)
        self.assertEqual(store.claim_max_attempts, 5)


class FailureReportsHowFarItGotTest(unittest.TestCase):
    """rowsParsed alone hid that a run stopped early.

    Both live failures reported rowsParsed=5000 for a 166,704-row file --
    technically true, and easy to read as "the file was small".
    """

    def test_a_failed_ingest_records_rows_parsed_before_failure(self) -> None:
        store = _Store(batch={**BATCH, "row_count": 166704})
        _run(store, wrote=False)
        failed = [p for _, p in store.updates if p.get("status") == INGEST_FAILED][0]
        self.assertIn("of 166,704", failed["last_error"])
        self.assertIn("not attempted", failed["last_error"])

    def test_an_incomplete_run_is_flagged_as_incomplete(self) -> None:
        store = _Store(batch={**BATCH, "row_count": 166704})
        _run(store, wrote=False)
        # 25 rows in the fixture CSV vs a claimed 166,704-row file.
        failed = [p for _, p in store.updates if p.get("status") == INGEST_FAILED][0]
        self.assertEqual(failed["rows_written"], 20)
        self.assertIn("catalog write failed after", failed["last_error"])

    def test_a_complete_run_is_not_flagged(self) -> None:
        store = _Store(batch={**BATCH, "row_count": 25})
        _run(store)
        self.assertEqual(store.statuses()[-1], INGESTED)


if __name__ == "__main__":
    unittest.main()


class ItReportsWhereTheTimeWentTest(unittest.TestCase):
    """#213 cut SCD2 versions by 97.8% and moved the clock by 3% per row.

    That ruled the SCD2 write out as the bottleneck without saying what the
    bottleneck is. SupabaseCatalogClient had been accumulating phase_seconds
    the whole time and nothing printed it, so the answer was being inferred
    from row counts. These tests exist so the numbers stay reported.
    """

    def _summary(self, **kwargs):
        """The summary reaches operators on stdout, so read it there.

        Deliberately not read off the batch row: catalog_download_batches has
        fixed columns and writing unknown keys to it would fail in production
        while passing against a dict-backed fake.
        """
        import contextlib
        import io
        import json

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _run(_Store(), **kwargs)
        printed = buffer.getvalue()
        start = printed.index("{", printed.rindex("\n{"))
        return json.loads(printed[start:printed.rindex("}") + 1])

    def test_every_phase_the_client_measured_is_reported(self) -> None:
        phases = self._summary()["phaseSeconds"]
        for phase in ("unchanged_detection", "catalog_upsert", "scd2_comparison",
                      "price_snapshot_insert", "scd2_close", "scd2_insert"):
            with self.subTest(phase=phase):
                self.assertIn(phase, phases)

    def test_the_phases_the_client_cannot_see_are_reported_too(self) -> None:
        """Storage, parsing and stamping happen outside the write client."""
        phases = self._summary()["phaseSeconds"]
        for phase in ("storage_download", "parse", "registry_stamp"):
            with self.subTest(phase=phase):
                self.assertIn(phase, phases)

    def test_the_biggest_phase_is_listed_first(self) -> None:
        """The report is read to find a bottleneck, so order by cost."""
        phases = self._summary()["phaseSeconds"]
        self.assertEqual(list(phases), sorted(phases, key=phases.get, reverse=True))
        self.assertEqual(next(iter(phases)), "catalog_upsert")

    def test_unclaimed_time_is_shown_rather_than_hidden(self) -> None:
        """Timers never sum to the total -- backoff sleeps, encoding, chunking.

        Reporting only the phases invites reading them as the whole picture,
        which is how you conclude the wrong thing dominates.
        """
        summary = self._summary()
        self.assertIn("phaseUnaccountedSeconds", summary)
        self.assertGreaterEqual(summary["phaseUnaccountedSeconds"], 0)

    def test_the_snapshot_write_is_counted_separately_from_the_version_write(self) -> None:
        """They shared a timer until 2026-09-09. One stayed, one went away."""
        phases = self._summary()["phaseSeconds"]
        self.assertNotEqual(phases["price_snapshot_insert"], phases["scd2_insert"])

    def test_snapshot_counts_ride_along(self) -> None:
        price_history = self._summary()["priceHistory"]
        self.assertEqual(price_history["inserted"], 11)
        self.assertEqual(price_history["duplicateSkipped"], 1)

    def test_a_failed_run_still_reports_its_phases(self) -> None:
        """A run that failed part-way is exactly when the split is wanted."""
        summary = self._summary(wrote=False)
        self.assertFalse(summary["success"])
        self.assertIn("phaseSeconds", summary)
        self.assertIn("catalog_upsert", summary["phaseSeconds"])

