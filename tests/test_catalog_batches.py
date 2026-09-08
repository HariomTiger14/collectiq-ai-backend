"""Batch vocabulary, state machine, leases and retention.

PR 1 of the staged refresh pipeline: schema and rules only, no downloader and
no ingester yet. These tests exist now so PR 2 and PR 3 are written against
one agreed definition rather than two scripts each half-remembering the same
strings -- which is how `_search_products` came to collapse a 404, a 429 and a
dropped connection into a single outcome.

Two properties are load-bearing and asserted hard:

  * The migration and the code cannot disagree. Every status the code can
    write must be in the CHECK constraint, or the first real batch fails at
    3am against a constraint nobody re-read.
  * A stale lease must always have somewhere to return to. `ops_cron_runs`
    has carried a tier3-sportscardspro-rotation row marked 'running' since
    2026-09-03 -- 118 hours -- because nothing reaps abandoned claims, and
    every "is anything running?" check has been wrong since.
"""

import pathlib
import re
import unittest
from datetime import datetime, timedelta, timezone

from scripts.catalog_batches import (
    ALL_ERROR_CLASSES,
    ALL_STATUSES,
    ALLOWED_TRANSITIONS,
    BLOCKED_HTTP,
    BUCKET,
    CLASS_BLOCKED,
    CLASS_TRANSIENT,
    DOWNLOADED,
    DOWNLOADING,
    FETCH_FAILED,
    IN_FLIGHT_STATUSES,
    INGEST_FAILED,
    INGESTED,
    INGESTING,
    LEASE_TIMEOUT_MINUTES,
    LEASED_STATUSES,
    PENDING,
    TERMINAL_STATUSES,
    TRANSIENT_HTTP,
    VALIDATED,
    VALIDATION_FAILED,
    WRITE_COPY,
    WRITE_NONE,
    WRITE_REST,
    assert_transition,
    can_transition,
    classify_http_failure,
    is_lease_stale,
    is_object_expired,
    new_batch_row,
    recovery_status,
    retention_expires_at,
    storage_key,
)

MIGRATION = pathlib.Path("database/migrations/20260908_catalog_download_batches.sql")
NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


class MigrationAndCodeAgreeTest(unittest.TestCase):
    """A mismatch here fails at 3am against a constraint nobody re-read."""

    def setUp(self) -> None:
        self.sql = MIGRATION.read_text()

    def test_every_code_status_is_allowed_by_the_check_constraint(self) -> None:
        check = re.search(
            r"catalog_download_batches_status_check CHECK \(status IN \((.*?)\)\)",
            self.sql, re.S)
        self.assertIsNotNone(check, "status CHECK constraint not found")
        allowed = set(re.findall(r"'([a-z_]+)'", check.group(1)))
        self.assertEqual(
            ALL_STATUSES - allowed, set(),
            "code can write a status the migration forbids")
        self.assertEqual(
            allowed - ALL_STATUSES, set(),
            "migration allows a status the code never uses")

    def test_every_error_class_is_allowed(self) -> None:
        check = re.search(
            r"error_class_check CHECK \(\s*last_error_class IS NULL OR last_error_class IN \((.*?)\)\s*\)",
            self.sql, re.S)
        self.assertIsNotNone(check)
        allowed = set(re.findall(r"'([a-z_]+)'", check.group(1)))
        self.assertEqual(ALL_ERROR_CLASSES, allowed)

    def test_every_write_path_is_allowed(self) -> None:
        check = re.search(r"write_path_check CHECK \((.*?)\)\s*\)", self.sql, re.S)
        self.assertIsNotNone(check)
        allowed = set(re.findall(r"'([a-z]+)'", check.group(1)))
        self.assertEqual({WRITE_REST, WRITE_COPY, WRITE_NONE}, allowed)

    def test_the_migration_is_additive_only(self) -> None:
        """No existing table may be touched by a table-creation migration."""
        for keyword in (r"\bDROP\b", r"\bDELETE\b", r"\bTRUNCATE\b",
                        r"\bUPDATE\b", r"\bALTER TABLE public\.pricecharting"):
            with self.subTest(keyword=keyword):
                self.assertIsNone(
                    re.search(keyword, self.sql, re.I),
                    f"migration contains {keyword}")

    def test_the_ingester_queue_index_exists(self) -> None:
        """Without it the ingester seq-scans a growing table every 10 minutes."""
        self.assertIn("catalog_download_batches_queue_idx", self.sql)
        # DOTALL: the index definition spans lines.
        self.assertIsNotNone(
            re.search(r"queue_idx.*?WHERE status = 'validated'", self.sql, re.S),
            "the queue index must be partial on status='validated', or the "
            "ingester seq-scans a growing table every 10 minutes")

    def test_rls_is_enabled(self) -> None:
        """Same posture as ops_cron_runs: service-role only."""
        self.assertIn("ENABLE ROW LEVEL SECURITY", self.sql)


class StateMachineTest(unittest.TestCase):
    def test_the_happy_path_is_walkable(self) -> None:
        path = [PENDING, DOWNLOADING, DOWNLOADED, VALIDATED, INGESTING, INGESTED]
        for current, target in zip(path, path[1:]):
            with self.subTest(step=f"{current}->{target}"):
                self.assertTrue(can_transition(current, target))

    def test_ingested_is_final(self) -> None:
        for target in ALL_STATUSES:
            with self.subTest(target=target):
                self.assertFalse(can_transition(INGESTED, target))

    def test_a_batch_cannot_skip_validation(self) -> None:
        """The guard is only a guard if it cannot be stepped around.

        A mistyped filter returned 200 with 123,166 rows of the WRONG catalog
        (measured 2026-09-07); downloaded -> ingesting would write it.
        """
        self.assertFalse(can_transition(DOWNLOADED, INGESTING))
        self.assertFalse(can_transition(DOWNLOADED, INGESTED))
        self.assertFalse(can_transition(PENDING, INGESTING))

    def test_a_validation_failure_is_final(self) -> None:
        """A wrong-catalog file is never retried into the write path."""
        for target in (INGESTING, VALIDATED, INGESTED):
            with self.subTest(target=target):
                self.assertFalse(can_transition(VALIDATION_FAILED, target))

    def test_a_failed_ingest_can_be_retried_in_place(self) -> None:
        """The object is still in storage, so a retry costs no vendor slot."""
        self.assertTrue(can_transition(INGEST_FAILED, INGESTING))

    def test_a_failed_fetch_is_not_resurrected(self) -> None:
        """Retried as a NEW batch, so the attempt history stays readable."""
        self.assertEqual(ALLOWED_TRANSITIONS[FETCH_FAILED], frozenset())

    def test_assert_transition_names_what_was_allowed(self) -> None:
        with self.assertRaises(ValueError) as caught:
            assert_transition(DOWNLOADED, INGESTED)
        self.assertIn("illegal batch transition", str(caught.exception))
        self.assertIn(VALIDATED, str(caught.exception))

    def test_terminal_and_in_flight_partition_every_status(self) -> None:
        self.assertEqual(TERMINAL_STATUSES | IN_FLIGHT_STATUSES, ALL_STATUSES)
        self.assertEqual(TERMINAL_STATUSES & IN_FLIGHT_STATUSES, frozenset())

    def test_every_transition_target_is_a_real_status(self) -> None:
        for current, targets in ALLOWED_TRANSITIONS.items():
            with self.subTest(status=current):
                self.assertIn(current, ALL_STATUSES)
                self.assertEqual(targets - ALL_STATUSES, frozenset())


class LeaseTest(unittest.TestCase):
    """A claim nobody releases is how a row stays 'running' for 118 hours."""

    def test_a_fresh_lease_is_not_stale(self) -> None:
        self.assertFalse(is_lease_stale(INGESTING, NOW - timedelta(minutes=1), now=NOW))

    def test_a_lease_past_the_window_is_stale(self) -> None:
        old = NOW - timedelta(minutes=LEASE_TIMEOUT_MINUTES + 1)
        self.assertTrue(is_lease_stale(INGESTING, old, now=NOW))

    def test_the_118_hour_case_is_caught(self) -> None:
        """The real one: a rotation row claimed 2026-09-03, never released."""
        self.assertTrue(is_lease_stale(INGESTING, NOW - timedelta(hours=118), now=NOW))

    def test_unleased_states_are_never_stale(self) -> None:
        for status in ALL_STATUSES - LEASED_STATUSES:
            with self.subTest(status=status):
                self.assertFalse(
                    is_lease_stale(status, NOW - timedelta(days=5), now=NOW))

    def test_a_missing_claim_time_is_not_stale(self) -> None:
        self.assertFalse(is_lease_stale(INGESTING, None, now=NOW))

    def test_every_leased_state_has_somewhere_to_return_to(self) -> None:
        """A reaper with nowhere to put a batch just moves the stuck row."""
        for status in LEASED_STATUSES:
            with self.subTest(status=status):
                target = recovery_status(status)
                self.assertIsNotNone(target)
                self.assertTrue(
                    can_transition(status, target),
                    f"reaper would make an illegal {status} -> {target} move")

    def test_an_abandoned_ingest_returns_to_validated_not_pending(self) -> None:
        """The file is still in storage; sending it back to pending would
        spend a vendor CSV slot re-downloading what we already have."""
        self.assertEqual(recovery_status(INGESTING), VALIDATED)

    def test_an_abandoned_download_returns_to_pending(self) -> None:
        self.assertEqual(recovery_status(DOWNLOADING), PENDING)


class HttpClassificationTest(unittest.TestCase):
    def test_vendor_health_statuses_are_transient(self) -> None:
        for status in sorted(TRANSIENT_HTTP):
            with self.subTest(status=status):
                self.assertEqual(classify_http_failure(status), CLASS_TRANSIENT)

    def test_403_is_blocked_not_transient(self) -> None:
        """Cloudflare refusing us is what killed the sportscardspro.com CSV
        path entirely -- retrying hardens a temporary block into a durable
        one."""
        self.assertEqual(classify_http_failure(403), CLASS_BLOCKED)
        self.assertNotIn(403, TRANSIENT_HTTP)
        self.assertIn(403, BLOCKED_HTTP)

    def test_unknown_and_missing_statuses_default_to_transient(self) -> None:
        """Retrying something unretryable is cheap; abandoning a healthy
        batch is not."""
        for status in (None, 418, 599, 400):
            with self.subTest(status=status):
                self.assertEqual(classify_http_failure(status), CLASS_TRANSIENT)


class StorageKeyTest(unittest.TestCase):
    def test_the_key_is_date_partitioned_and_batch_named(self) -> None:
        key = storage_key("sportscardspro", "abc-123", now=NOW)
        self.assertEqual(key, "sportscardspro/2026/09/08/abc-123.csv")

    def test_the_source_leads_so_a_second_source_partitions_cleanly(self) -> None:
        """completed-categories is meant to join as source #2 with no schema
        change; the key must not assume sportscardspro."""
        self.assertTrue(
            storage_key("completed-categories", "x", now=NOW).startswith("completed-categories/"))

    def test_the_bucket_is_dedicated(self) -> None:
        """Not collectiq-portfolio-images: that holds user data, and bulk
        vendor dumps have a different lifecycle and size class."""
        self.assertEqual(BUCKET, "catalog-refresh-batches")
        self.assertNotIn("portfolio", BUCKET)


class RetentionTest(unittest.TestCase):
    def test_an_ingested_file_is_disposable_within_a_day(self) -> None:
        self.assertEqual(retention_expires_at(INGESTED, NOW), NOW + timedelta(days=1))

    def test_failed_files_are_kept_a_week_because_they_are_the_evidence(self) -> None:
        for status in (INGEST_FAILED, VALIDATION_FAILED):
            with self.subTest(status=status):
                self.assertEqual(
                    retention_expires_at(status, NOW), NOW + timedelta(days=7))

    def test_an_in_flight_batch_is_never_swept(self) -> None:
        for status in IN_FLIGHT_STATUSES:
            with self.subTest(status=status):
                self.assertIsNone(retention_expires_at(status, NOW))
                self.assertFalse(is_object_expired(status, NOW - timedelta(days=30), now=NOW))

    def test_expiry_is_inclusive_at_the_boundary(self) -> None:
        self.assertTrue(is_object_expired(INGESTED, NOW - timedelta(days=1), now=NOW))
        self.assertFalse(
            is_object_expired(INGESTED, NOW - timedelta(days=1) + timedelta(seconds=1), now=NOW))


class NewBatchRowTest(unittest.TestCase):
    def test_a_new_batch_starts_pending_with_its_counts(self) -> None:
        row = new_batch_row(
            source="sportscardspro", console_uids=["G1", "G2"], registry_ids=["r1", "r2"])
        self.assertEqual(row["status"], PENDING)
        self.assertEqual(row["requested_count"], 2)
        self.assertEqual(row["attempts"], 0)

    def test_it_writes_no_column_the_migration_lacks(self) -> None:
        sql = MIGRATION.read_text()
        row = new_batch_row(source="s", console_uids=["a"], registry_ids=["b"])
        for column in row:
            with self.subTest(column=column):
                self.assertIn(column, sql)


if __name__ == "__main__":
    unittest.main()
