"""The download stage: claim, fetch, validate, upload — and nothing else.

PR 2 writes no catalog rows, no history and no registry stamps. Several tests
below exist only to prove that, because the failure they guard against is
silent: a batch marked refreshed from data that never landed looks identical
to a successful one until someone reads the prices.

The guards worth breaking a build over:

  * validation runs BEFORE upload, so a wrong-catalog file never enters the
    ingest queue. A mistyped filter returned HTTP 200 with 123,166 rows of
    the wrong catalog (measured 2026-09-07).
  * a 403 stops the run. Cloudflare refusing us is what killed the
    sportscardspro.com CSV path entirely; retrying hardens a temporary block.
  * backpressure. A stuck ingester must not turn into a storage bill.
  * --commit is required. Default-safe, because this job spends vendor CSV
    slots (144/day, sports wants ~106).
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from scripts.catalog_batches import (
    DOWNLOADED,
    DOWNLOADING,
    FETCH_FAILED,
    IN_FLIGHT_STATUSES,
    INGESTED,
    INGESTING,
    PENDING,
    QUEUE_DEPTH_STATUSES,
    VALIDATED,
    VALIDATION_FAILED,
)
from scripts.download_catalog_batch import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_QUEUE_DEPTH,
    BatchStore,
    main,
    parse_args,
    reap_stale_leases,
)

CSV = "id,console-name,product-name,loose-price\n" + "".join(
    f"{i},Baseball Cards 1962 Bazooka,Card {i},$1.00\n" for i in range(1, 21)
)
# The 2026-09-13 incident in miniature: the right NUMBER of families (so the
# count check passes -- this is not the 123k video-game dump), with exactly one
# family the requested sets do not account for. The vendor renamed G9533 from
# "2015 Topps Platinum Autograph Rookies" to the name below, and one unmatched
# family refuses the whole file.
WRONG_FAMILY_CSV = "id,console-name,product-name,loose-price\n" + "".join(
    f"{i},Baseball Cards 1962 Bazooka,Card {i},$1.00\n" for i in range(1, 11)
) + "".join(
    f"{i},Football Cards 2015 Topps Platinum Autographed Rookie Refractor,"
    f"Card {i},$1.00\n" for i in range(11, 14)
)

WRONG_CATALOG = "id,console-name,product-name,loose-price\n" + "".join(
    f"{i},Console {i % 200},Game {i},$1.00\n" for i in range(1, 400)
)


class _Store:
    """Stands in for BatchStore, recording every write."""

    # Mirrors BatchStore. A fake missing a method its caller reads fails at
    # runtime and nowhere else -- the same gap that let 39 mock handlers go
    # green in #212.
    sibling_names: list[str] = []

    def sibling_set_names(self, *, source, uids):
        return list(self.sibling_names)

    def __init__(self, *, due=None, batches=None):
        self._due = due if due is not None else [
            {"registry_id": f"r{i}", "console_uid": f"G{i}", "set_name": "1962 Bazooka"}
            for i in range(3)
        ]
        self._batches = batches or {}
        self.inserted: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        self.uploads: list[tuple[str, Path]] = []

    def claim_due_sets(self, *, source, limit):
        return self._due[:limit]

    def batches(self, *, source, statuses):
        return [b for b in self._batches.get("all", []) if b["status"] in statuses]

    def insert(self, row):
        # created_at is stamped by a column default in Postgres, so a real
        # inserted row always has one. Without it here the cooldown check --
        # which reads created_at -- silently saw no recent failures and every
        # test of it passed while blocking nothing.
        batch = {**row, "batch_id": "batch-1", "attempts": 0,
                 "created_at": datetime.now(timezone.utc).isoformat()}
        self.inserted.append(batch)
        # Visible to batches() afterwards, as a real insert would be.
        self._batches.setdefault("all", []).append(batch)
        return batch

    def update(self, batch_id, patch):
        self.updates.append((batch_id, patch))
        # APPLIES the patch, like BatchStore does. Recording without applying
        # made the fake amnesiac: a second run re-read the batch exactly as the
        # first one found it, so any test that runs the script twice on one row
        # -- which is the only way to see a counter accumulate -- passed while
        # testing nothing.
        for row in self._batches.get("all", []):
            if row.get("batch_id") == batch_id:
                row.update(patch)
                break

    def upload(self, key, path):
        self.uploads.append((key, path))

    # helpers
    def statuses(self):
        return [p.get("status") for _, p in self.updates if "status" in p]


def _run(store, *, body=CSV, status=200, argv=("--commit",), calls=None):
    """`calls` collects every vendor request made.

    uploads is NOT a proxy for "spent a vendor slot": a batch that fails
    validation has already paid for its CSV and then never uploads. Counting
    fetches is the only way to see the slot burn.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if status != 200:
            return httpx.Response(status, text="upstream error")
        return httpx.Response(200, content=body.encode())

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*a, **kw):
        kw.pop("transport", None)
        return real_client(*a, transport=transport, **{k: v for k, v in kw.items()})

    with patch("scripts.download_catalog_batch.BatchStore", return_value=store), \
         patch("scripts.download_catalog_batch.httpx.Client", side_effect=client_factory), \
         patch("scripts.download_catalog_batch.SharedRateLimiter") as limiter, \
         patch.dict("os.environ", {"SUPABASE_URL": "https://x.test",
                                   "SUPABASE_SERVICE_ROLE_KEY": "k",
                                   "PRICECHARTING_API_TOKEN": "t"}):
        limiter.return_value.acquire.return_value = True
        return main(list(argv))


class DefaultSafeTest(unittest.TestCase):
    def test_without_commit_nothing_is_claimed_or_written(self) -> None:
        """This job spends vendor CSV slots; the default must not."""
        store = _Store()
        _run(store, argv=())
        self.assertEqual(store.inserted, [])
        self.assertEqual(store.updates, [])
        self.assertEqual(store.uploads, [])

    def test_commit_is_off_by_default_in_the_parser(self) -> None:
        self.assertFalse(parse_args([]).commit)

    def test_the_default_batch_size_is_350(self) -> None:
        """500 works but sits near the vendor's 503 cliff; 1000 fails."""
        self.assertEqual(DEFAULT_BATCH_SIZE, 350)
        self.assertEqual(parse_args([]).batch_size, 350)


class HappyPathTest(unittest.TestCase):
    def test_the_batch_walks_pending_to_validated_and_uploads(self) -> None:
        store = _Store()
        self.assertEqual(_run(store), 0)
        self.assertEqual(store.statuses(), [DOWNLOADING, DOWNLOADED, VALIDATED])
        self.assertEqual(len(store.uploads), 1)

    def test_the_storage_key_is_recorded_with_the_validated_status(self) -> None:
        store = _Store()
        _run(store)
        final = [p for _, p in store.updates if p.get("status") == VALIDATED][0]
        self.assertIn("storage_key", final)
        self.assertTrue(final["storage_key"].startswith("sportscardspro/"))

    def test_the_lease_is_released_on_success(self) -> None:
        store = _Store()
        _run(store)
        final = [p for _, p in store.updates if p.get("status") == VALIDATED][0]
        self.assertIsNone(final["claimed_at"])


class NothingIsIngestedOrStampedTest(unittest.TestCase):
    """PR 2 downloads. Stamping belongs to the ingester, after a clean write."""

    def test_no_registry_write_of_any_kind(self) -> None:
        store = _Store()
        _run(store)
        for _, patch_body in store.updates:
            for key in patch_body:
                with self.subTest(key=key):
                    self.assertFalse(
                        key.startswith("tier3_") or key.startswith("tier1_"),
                        f"the downloader wrote a registry column: {key}")

    def test_the_script_never_mentions_the_catalog_write_path(self) -> None:
        source = Path("scripts/download_catalog_batch.py").read_text()
        for forbidden in ("to_catalog_row", "write_catalog_rows",
                          "mark_tier3_refreshed", "SupabaseCatalogClient"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, source)


class ValidationGateTest(unittest.TestCase):
    def test_a_wrong_catalog_file_is_never_uploaded(self) -> None:
        """The whole reason validation precedes upload."""
        store = _Store()
        _run(store, body=WRONG_CATALOG)
        self.assertEqual(store.uploads, [], "a wrong-catalog file reached the bucket")
        self.assertIn(VALIDATION_FAILED, store.statuses())
        self.assertNotIn(VALIDATED, store.statuses())

    def test_the_mismatch_detail_is_recorded_for_diagnosis(self) -> None:
        store = _Store()
        _run(store, body=WRONG_CATALOG)
        failed = [p for _, p in store.updates if p.get("status") == VALIDATION_FAILED][0]
        self.assertEqual(failed["last_error_class"], "validation")
        detail = json.loads(failed["last_error"])
        self.assertIn("observedFamilies", detail)
        self.assertIn("expectedSetNames", detail)


class FetchFailureTest(unittest.TestCase):
    def test_a_503_marks_fetch_failed_and_exits_zero(self) -> None:
        """Transient: the run ends quietly and the sets stay queued."""
        store = _Store()
        self.assertEqual(_run(store, status=503), 0)
        self.assertIn(FETCH_FAILED, store.statuses())
        failed = [p for _, p in store.updates if p.get("status") == FETCH_FAILED][0]
        self.assertEqual(failed["last_error_class"], "transient")

    def test_a_403_exits_non_zero_so_the_block_is_visible(self) -> None:
        """Cloudflare refusing us killed the sportscardspro.com CSV path.
        Retrying hardens a temporary block into a durable one."""
        store = _Store()
        self.assertEqual(_run(store, status=403), 1)
        failed = [p for _, p in store.updates if p.get("status") == FETCH_FAILED][0]
        self.assertEqual(failed["last_error_class"], "blocked")

    def test_no_upload_happens_on_a_failed_fetch(self) -> None:
        store = _Store()
        _run(store, status=503)
        self.assertEqual(store.uploads, [])

    def test_a_429_is_transient_not_blocked(self) -> None:
        store = _Store()
        _run(store, status=429)
        failed = [p for _, p in store.updates if p.get("status") == FETCH_FAILED][0]
        self.assertEqual(failed["last_error_class"], "transient")


class BackpressureTest(unittest.TestCase):
    """A stuck ingester must not become a storage bill."""

    def test_a_full_queue_skips_the_run_without_fetching(self) -> None:
        store = _Store(batches={"all": [
            {"batch_id": "a", "status": VALIDATED, "requested_count": 350},
            {"batch_id": "b", "status": VALIDATED, "requested_count": 350},
        ]})
        self.assertEqual(_run(store), 0)
        self.assertEqual(store.inserted, [], "claimed sets while the queue was full")
        self.assertEqual(store.uploads, [])

    def test_one_queued_batch_still_allows_a_download(self) -> None:
        store = _Store(batches={"all": [
            {"batch_id": "a", "status": VALIDATED, "requested_count": 350},
        ]})
        _run(store)
        self.assertEqual(len(store.uploads), 1)


class SingleWriterBackpressureTest(unittest.TestCase):
    """--max-queue-depth 1, the setting the 10-minute sports cron runs with.

    The depth count used to be {DOWNLOADED, VALIDATED} -- the files sitting on
    disk. That answers "is the ingester behind?", which is the question the flag
    was written for. It does not answer "is anything writing to disk right now?",
    and on a 10-minute cadence those come apart: a 270k-row ingest takes ~7.5
    minutes, so the next tick arrives while the previous batch is INGESTING and
    invisible to the count. The downloader then writes a second 37 MB file while
    the first is still being read.

    Nothing downstream would report that. Both runs succeed, both ledger rows are
    green, and the only symptom is Disk IOPS -- which is not observable from
    inside Postgres.
    """

    def _run_at_depth_one(self, status):
        store = _Store(batches={"all": [
            {"batch_id": "a", "status": status, "requested_count": 350},
        ]})
        code = _run(store, argv=("--commit", "--max-queue-depth", "1"))
        return store, code

    def test_one_ingesting_batch_blocks_the_download(self) -> None:
        """The case the old count missed entirely."""
        store, code = self._run_at_depth_one(INGESTING)
        self.assertEqual(code, 0)
        self.assertEqual(store.uploads, [], "downloaded while an ingest was writing")
        self.assertEqual(store.inserted, [], "claimed sets while an ingest was writing")

    def test_one_downloading_batch_blocks_the_download(self) -> None:
        """A live download is a disk writer too.

        claimed_at is left unset so the reaper cannot mistake it for a dead
        lease -- this asserts the queue count, not the reaper.
        """
        store, code = self._run_at_depth_one(DOWNLOADING)
        self.assertEqual(code, 0)
        self.assertEqual(store.uploads, [])
        self.assertEqual(store.inserted, [])

    def test_one_validated_batch_blocks_the_download(self) -> None:
        store, code = self._run_at_depth_one(VALIDATED)
        self.assertEqual(code, 0)
        self.assertEqual(store.uploads, [])
        self.assertEqual(store.inserted, [])

    def test_one_downloaded_batch_blocks_the_download(self) -> None:
        store, code = self._run_at_depth_one(DOWNLOADED)
        self.assertEqual(code, 0)
        self.assertEqual(store.uploads, [])
        self.assertEqual(store.inserted, [])

    def test_an_empty_queue_still_downloads_at_depth_one(self) -> None:
        """Depth 1 must not mean 'never download'."""
        store = _Store(batches={"all": []})
        _run(store, argv=("--commit", "--max-queue-depth", "1"))
        self.assertEqual(len(store.uploads), 1)

    def test_an_ingested_batch_does_not_block(self) -> None:
        """Terminal states are not work in flight; every past batch is INGESTED,
        so counting them would wedge the rotation permanently after run one."""
        store = _Store(batches={"all": [
            {"batch_id": "a", "status": INGESTED, "requested_count": 350},
        ]})
        _run(store, argv=("--commit", "--max-queue-depth", "1"))
        self.assertEqual(len(store.uploads), 1)

    def test_the_default_is_still_two_so_a_shell_run_is_unchanged(self) -> None:
        """The flag changes what is counted for everyone; the default must not
        change who is blocked. A manual Shell run with one batch in flight
        behaved one way before this change and must behave the same way after."""
        self.assertEqual(DEFAULT_MAX_QUEUE_DEPTH, 2)
        store = _Store(batches={"all": [
            {"batch_id": "a", "status": INGESTING, "requested_count": 350},
        ]})
        _run(store)
        self.assertEqual(len(store.uploads), 1)


class QueueDepthStatusesTest(unittest.TestCase):
    def test_the_counted_statuses_are_exactly_the_in_flight_ones(self) -> None:
        """Expressed against IN_FLIGHT_STATUSES rather than a second literal
        list, so a new state added to the pipeline cannot be silently left out
        of the backpressure count.

        PENDING is the one in-flight state deliberately excluded: it holds
        claimed uids but has no file and writes nothing, and the resume path
        below depends on a run reaching it rather than being turned away.
        """
        self.assertEqual(QUEUE_DEPTH_STATUSES, IN_FLIGHT_STATUSES - {PENDING})

    def test_a_pending_batch_does_not_block_its_own_resume(self) -> None:
        """The resume path exists to pick a PENDING batch back up. Counting it
        toward the queue would make depth 1 refuse the very run meant to clear
        it, and those 350 uids would stay booked forever."""
        self.assertNotIn(PENDING, QUEUE_DEPTH_STATUSES)


class ResumeTest(unittest.TestCase):
    """An abandoned batch is resumed, not re-claimed.

    Claiming the same sets again would double-book them and spend a second
    vendor slot on rows already spoken for.
    """

    def test_a_pending_batch_is_resumed_instead_of_claiming_new_sets(self) -> None:
        store = _Store(batches={"all": [
            {"batch_id": "old-1", "status": PENDING, "console_uids": ["G1", "G2"],
             "registry_ids": ["r1", "r2"], "requested_count": 2, "attempts": 1},
        ]})
        _run(store)
        self.assertEqual(store.inserted, [], "created a new batch instead of resuming")
        self.assertTrue(all(bid == "old-1" for bid, _ in store.updates))

    def test_the_download_counter_advances_on_resume(self) -> None:
        """download_attempts, not `attempts`.

        `attempts` is an ingest alias since the counters were split: bumping
        it here is what let a download spend the ingester's retry budget, so a
        Storage object that could still be retried was abandoned early.
        """
        store = _Store(batches={"all": [
            {"batch_id": "old-1", "status": PENDING, "console_uids": ["G1"],
             "registry_ids": ["r1"], "requested_count": 1,
             "download_attempts": 1},
        ]})
        _run(store)
        # Asserted on the EFFECT, not on which payload carries it: since the
        # slot-refusal fix the bump lands after acquire() rather than in the
        # PENDING->DOWNLOADING claim.
        bumps = [p["download_attempts"] for _, p in store.updates
                 if "download_attempts" in p]
        self.assertEqual(bumps, [2])
        for _, payload in store.updates:
            self.assertNotIn("attempts", payload,
                             "the download bumped the ingest alias")


class SlotRefusalDoesNotSpendAnAttemptTest(unittest.TestCase):
    """A refused CSV slot is not a failed download.

    refresh_completed_pricecharting_categories holds the shared
    pricecharting:csv slot from 04:45 to ~08:29 UTC (23 calls at 610s) and
    outranks sports -- it declares essential_categories, the downloader
    declares tier3, which is bulk. So every sports tick in that window is
    refused before it ever reaches the vendor.

    While the attempt was charged at claim time, five refusals took the row to
    the --max-attempts ceiling and the NEXT run stopped the whole rotation with
    download_attempts_exhausted and exit 1, until a human reset the row. A hard
    stop produced entirely by backpressure working correctly, and it would have
    fired on the first morning the categories job was resumed.
    """

    def _pending_row(self, attempts=0):
        return {"batch_id": "old-1", "status": PENDING, "console_uids": ["G1"],
                "registry_ids": ["r1"], "requested_count": 1,
                "download_attempts": attempts}

    def _run_refused(self, store):
        """One run where the limiter grants nothing."""
        with patch("scripts.download_catalog_batch.SharedRateLimiter") as limiter:
            limiter.return_value.acquire.return_value = False
            with patch("scripts.download_catalog_batch.BatchStore", return_value=store), \
                 patch.dict("os.environ", {"SUPABASE_URL": "https://x.test",
                                           "SUPABASE_SERVICE_ROLE_KEY": "k",
                                           "PRICECHARTING_API_TOKEN": "t"}):
                return main(["--commit"])

    def test_one_refusal_spends_nothing(self) -> None:
        store = _Store(batches={"all": [self._pending_row(0)]})
        self.assertEqual(self._run_refused(store), 0)
        row = store._batches["all"][0]
        self.assertEqual(row["download_attempts"], 0)
        self.assertEqual(row["status"], PENDING)

    def test_ten_refusals_leave_the_counter_untouched(self) -> None:
        """Ten is more than --max-attempts 5, and more than the ~22 ticks the
        real window would produce only in that it is enough to prove the
        counter does not creep."""
        store = _Store(batches={"all": [self._pending_row(0)]})
        for _ in range(10):
            self.assertEqual(self._run_refused(store), 0)
        self.assertEqual(store._batches["all"][0]["download_attempts"], 0)

    def test_the_rotation_still_runs_after_a_morning_of_refusals(self) -> None:
        """The whole point. After the categories job releases the slot, the
        next tick must download -- not stop with download_attempts_exhausted."""
        store = _Store(batches={"all": [self._pending_row(0)]})
        for _ in range(10):
            self._run_refused(store)
        self.assertEqual(_run(store), 0, "the rotation halted after refusals")
        self.assertEqual(len(store.uploads), 1, "no download after the slot freed up")

    def test_a_refusal_does_not_touch_the_ingest_alias_either(self) -> None:
        store = _Store(batches={"all": [self._pending_row(0)]})
        self._run_refused(store)
        for _, payload in store.updates:
            self.assertNotIn("attempts", payload)
            self.assertNotIn("ingest_attempts", payload)

    def test_the_lease_is_still_taken_and_released_around_the_wait(self) -> None:
        """Not bumping the counter must not mean not claiming the batch: the
        limiter can wait up to 30 minutes, and an unleased row is one another
        run can claim underneath us."""
        store = _Store(batches={"all": [self._pending_row(0)]})
        self._run_refused(store)
        statuses = [p.get("status") for _, p in store.updates if "status" in p]
        self.assertEqual(statuses, [DOWNLOADING, PENDING])
        released = [p for _, p in store.updates if p.get("status") == PENDING][0]
        self.assertIsNone(released["claimed_at"])
        self.assertIsNone(released["claimed_by"])
        self.assertEqual(released["last_error_class"], "transient")

    def test_a_real_download_still_spends_an_attempt(self) -> None:
        """The ceiling is not removed, only moved past the limiter. A run that
        reaches the vendor is charged exactly as before."""
        store = _Store(batches={"all": [self._pending_row(1)]})
        _run(store)
        self.assertEqual(store._batches["all"][0]["download_attempts"], 2)

    def test_a_spent_row_still_halts_the_rotation(self) -> None:
        """download_attempts_exhausted stays exit 1 for real failures."""
        store = _Store(batches={"all": [self._pending_row(5)]})
        self.assertEqual(_run(store), 1)
        self.assertEqual(store.uploads, [])


class ValidationFailureCooldownTest(unittest.TestCase):
    """A refused combination must not be re-requested every ten minutes.

    2026-09-13, 20:50 to 22:10 UTC: nine consecutive batches, the same 350
    console_uids, the same 203,560 rows, the same single unexpected family
    (`football cards 2015 topps platinum autographed rookie refractor`, uid
    G9533, which the vendor renamed from the registry's "2015 Topps Platinum
    Autograph Rookies"). Each burned one of the day's 141 account-wide CSV
    slots to re-download a file already known to be refused.

    The guard did its job: one unmatched family fails the whole file, closed.
    What was missing is that a refusal left no trace the NEXT run could see --
    validation_failed never stamps its sets, and claim_due_sets orders
    NULLS FIRST, so the identical set is exactly what comes back.
    """

    UIDS = ["G1", "G2", "G3"]

    def _due(self):
        return [{"registry_id": f"r{i}", "console_uid": uid,
                 "set_name": "1962 Bazooka"} for i, uid in enumerate(self.UIDS)]

    def _failed_batch(self, *, uids, age_hours):
        when = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        return {"batch_id": "failed-1", "status": VALIDATION_FAILED,
                "console_uids": list(uids), "requested_count": len(uids),
                "created_at": when.isoformat()}

    def test_the_same_combination_is_refused_without_spending_a_slot(self) -> None:
        store = _Store(due=self._due(), batches={"all": [
            self._failed_batch(uids=self.UIDS, age_hours=0.1)]})
        self.assertEqual(_run(store), 1, "a stopped rotation must not exit 0")
        self.assertEqual(store.uploads, [], "downloaded a known-refused file")
        self.assertEqual(store.inserted, [], "created a batch row for it anyway")

    def test_a_different_combination_still_downloads(self) -> None:
        """The block is on the combination, not on the sets. 349 of 350 are
        innocent and must not be punished for a neighbour's rename."""
        store = _Store(due=self._due(), batches={"all": [
            self._failed_batch(uids=["G1", "G2", "G9"], age_hours=0.1)]})
        self.assertEqual(_run(store), 0)
        self.assertEqual(len(store.uploads), 1)

    def test_an_overlapping_combination_is_not_blocked(self) -> None:
        """Equality, not overlap: the rotation moving on by one set is a real
        new batch, and no threshold has to be guessed."""
        store = _Store(due=self._due(), batches={"all": [
            self._failed_batch(uids=["G1", "G2"], age_hours=0.1)]})
        self.assertEqual(_run(store), 0)
        self.assertEqual(len(store.uploads), 1)

    def test_the_block_expires(self) -> None:
        store = _Store(due=self._due(), batches={"all": [
            self._failed_batch(uids=self.UIDS, age_hours=99)]})
        self.assertEqual(_run(store), 0)
        self.assertEqual(len(store.uploads), 1)

    def test_zero_hours_disables_the_block(self) -> None:
        store = _Store(due=self._due(), batches={"all": [
            self._failed_batch(uids=self.UIDS, age_hours=0.0)]})
        _run(store, argv=("--commit", "--validation-cooldown-hours", "0"))
        self.assertEqual(len(store.uploads), 1)

    def test_nine_reclaims_cost_one_download_not_nine(self) -> None:
        """The shape of the incident, end to end."""
        store = _Store(due=self._due(), batches={"all": []})
        calls: list[str] = []
        _run(store, body=WRONG_FAMILY_CSV, calls=calls)
        self.assertEqual(len(calls), 1, "the first attempt should fetch")
        failed = [p for _, p in store.updates
                  if p.get("status") == VALIDATION_FAILED]
        self.assertEqual(len(failed), 1, "the batch did not fail validation")
        for _ in range(8):
            self.assertEqual(_run(store, body=WRONG_FAMILY_CSV, calls=calls), 1)
        self.assertEqual(len(calls), 1,
                         f"spent {len(calls)} vendor CSV slots on a combination "
                         f"already known to be refused")

    def test_a_successful_batch_does_not_arm_the_block(self) -> None:
        store = _Store(due=self._due(), batches={"all": [
            {"batch_id": "ok-1", "status": INGESTED, "console_uids": self.UIDS,
             "created_at": datetime.now(timezone.utc).isoformat()}]})
        self.assertEqual(_run(store), 0)
        self.assertEqual(len(store.uploads), 1)


class ReaperTest(unittest.TestCase):
    """A claim nobody releases is how a row stays 'running' for 118 hours."""

    def test_a_stale_download_lease_is_returned_to_pending(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        store = _Store(batches={"all": [
            {"batch_id": "stuck", "status": DOWNLOADING, "claimed_at": old},
        ]})
        reaped = reap_stale_leases(store, source="sportscardspro", commit=True)
        self.assertEqual(reaped, 1)
        self.assertEqual(store.updates[0][1]["status"], PENDING)
        self.assertIsNone(store.updates[0][1]["claimed_at"])

    def test_a_fresh_lease_is_left_alone(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        store = _Store(batches={"all": [
            {"batch_id": "busy", "status": DOWNLOADING, "claimed_at": recent},
        ]})
        self.assertEqual(reap_stale_leases(store, source="sportscardspro", commit=True), 0)
        self.assertEqual(store.updates, [])

    def test_the_reaper_writes_nothing_without_commit(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        store = _Store(batches={"all": [
            {"batch_id": "stuck", "status": DOWNLOADING, "claimed_at": old},
        ]})
        self.assertEqual(reap_stale_leases(store, source="sportscardspro", commit=False), 1)
        self.assertEqual(store.updates, [])


class RateLimiterTest(unittest.TestCase):
    def test_no_slot_leaves_the_batch_pending_rather_than_failed(self) -> None:
        """Not getting a turn is not a failure of the batch."""
        store = _Store()
        with patch("scripts.download_catalog_batch.BatchStore", return_value=store), \
             patch("scripts.download_catalog_batch.SharedRateLimiter") as limiter, \
             patch.dict("os.environ", {"SUPABASE_URL": "https://x.test",
                                       "SUPABASE_SERVICE_ROLE_KEY": "k",
                                       "PRICECHARTING_API_TOKEN": "t"}):
            limiter.return_value.acquire.return_value = False
            self.assertEqual(main(["--commit"]), 0)
        self.assertEqual(store.statuses()[-1], PENDING)
        self.assertEqual(store.uploads, [])

    def test_the_limiter_is_used_at_all(self) -> None:
        """Manual probes bypassed it; production must not."""
        source = Path("scripts/download_catalog_batch.py").read_text()
        self.assertIn("SharedRateLimiter", source)
        self.assertIn("acquire(", source)


if __name__ == "__main__":
    unittest.main()


class ARefusedBatchExitsNonZeroTest(unittest.TestCase):
    """Two refusals on 2026-09-09 were recorded green, two minutes apart.

    A refusal is not backpressure. The file is a wrong-catalog response, the
    same sets are claimed again next run, and it repeats until someone looks --
    so it must be loud at the Render level too, not only in the summary.

    A vendor 503 stays a quiet zero: that is expected pacing, and alerting on
    it would train the alert away.
    """

    def test_the_refusal_path_returns_one(self) -> None:
        import pathlib
        import re

        source = pathlib.Path("scripts/download_catalog_batch.py").read_text()
        block = source[source.index("except CsvFamilyMismatch"):]
        block = block[: block.index("key = storage_key")]
        self.assertRegex(block, r"return 1\b",
                         "a refused batch still exits 0; it was recorded green twice")

    def test_a_transient_fetch_failure_stays_quiet(self) -> None:
        """Only a blocked host exits non-zero on the fetch path."""
        import pathlib

        source = pathlib.Path("scripts/download_catalog_batch.py").read_text()
        self.assertIn("return 1 if error_class == CLASS_BLOCKED else 0", source)


class DuplicateConsoleUidDoesNotJamTheRotationTest(unittest.TestCase):
    """Asserted through main(), not against the helpers.

    dedupe_by_console_uid and the sibling lookup are both unit-tested in
    tests/test_duplicate_console_uid.py, and both passed while mutations that
    removed them from the DOWNLOADER entirely went unnoticed. The call site is
    where the jam actually happens.
    """

    DUPE = [
        {"registry_id": "r1", "console_uid": "G9157", "set_name": "2015 Panini Donrus"},
        {"registry_id": "r2", "console_uid": "G9157", "set_name": "2015 Panini Donruss"},
        {"registry_id": "r3", "console_uid": "G1", "set_name": "1962 Bazooka"},
    ]

    def test_the_uid_is_requested_only_once(self) -> None:
        """Sent twice, one family comes back, validation passes, and BOTH rows
        are stamped refreshed -- the duplicate having never been refreshed."""
        store = _Store(due=list(self.DUPE))
        _run(store)
        uids = store.inserted[0]["console_uids"]
        self.assertEqual(sorted(uids), ["G1", "G9157"],
                         f"the duplicate uid was requested twice: {uids}")

    def test_only_the_kept_row_is_claimed(self) -> None:
        store = _Store(due=list(self.DUPE))
        _run(store)
        self.assertEqual(sorted(store.inserted[0]["registry_ids"]), ["r1", "r3"])

    def test_the_vendors_own_label_does_not_refuse_the_batch(self) -> None:
        """The jam: claim 'Donrus', get answered 'Donruss'. Without the sibling
        widening this refuses all 350 sets and reclaims them next run."""
        store = _Store(due=[self.DUPE[0]])
        store.sibling_names = ["2015 Panini Donrus", "2015 Panini Donruss"]
        csv = ("id,console-name,product-name,loose-price\n"
               "1,Football Cards 2015 Panini Donruss,Card,1.00\n")
        code = _run(store, body=csv)
        statuses = [p.get("status") for _, p in store.updates]
        self.assertNotIn("validation_failed", statuses,
                         "the vendor's own spelling was refused as a wrong catalog")
        self.assertEqual(code, 0)

    def test_a_wrong_catalog_is_still_refused_with_siblings_present(self) -> None:
        """Widening must not admit a family belonging to no requested uid."""
        store = _Store(due=[self.DUPE[0]])
        store.sibling_names = ["2015 Panini Donrus", "2015 Panini Donruss"]
        csv = ("id,console-name,product-name,loose-price\n"
               "1,Baseball Cards 1962 Topps,Card,1.00\n")
        code = _run(store, body=csv)
        statuses = [p.get("status") for _, p in store.updates]
        self.assertIn("validation_failed", statuses,
                      "a wrong catalog passed once siblings were allowed")
        self.assertEqual(code, 1, "a refusal must exit non-zero")


class AResumedBatchIsValidatedToo(unittest.TestCase):
    """The resume path had no family check at all.

    Expected names were built from the CLAIMED rows, and a resumed PENDING
    batch has none -- so set_names was empty, and validate_csv_families returns
    early when it has nothing to compare against. Every retry therefore ran
    with the wrong-catalog guard switched off.

    That is worse than the G9157 jam it sits beside: a jam refuses a good batch
    loudly, an absent guard accepts a bad one quietly. Both are fixed by
    deriving the names from the batch's own console_uids, which exist on both
    paths.
    """

    PENDING_BATCH = {"batch_id": "old-1", "status": PENDING,
                     "console_uids": ["G9157"], "registry_ids": ["r1"],
                     "requested_count": 1, "attempts": 0}

    def test_a_resumed_batch_accepts_the_vendors_own_label(self) -> None:
        """Claim 'Donrus', crash, resume, get answered 'Donruss'."""
        store = _Store(batches={"all": [dict(self.PENDING_BATCH)]})
        store.sibling_names = ["2015 Panini Donrus", "2015 Panini Donruss"]
        csv = ("id,console-name,product-name,loose-price\n"
               "1,Football Cards 2015 Panini Donruss,Card,1.00\n")
        code = _run(store, body=csv)
        statuses = [p.get("status") for _, p in store.updates]
        self.assertNotIn("validation_failed", statuses)
        self.assertEqual(code, 0)

    def test_a_resumed_batch_still_refuses_a_wrong_catalog(self) -> None:
        """The guard that was absent entirely before this."""
        store = _Store(batches={"all": [dict(self.PENDING_BATCH)]})
        store.sibling_names = ["2015 Panini Donrus", "2015 Panini Donruss"]
        csv = ("id,console-name,product-name,loose-price\n"
               "1,Baseball Cards 1962 Topps,Card,1.00\n")
        code = _run(store, body=csv)
        statuses = [p.get("status") for _, p in store.updates]
        self.assertIn("validation_failed", statuses,
                      "a resumed batch accepted a wrong catalog")
        self.assertEqual(code, 1)

    def test_the_resumed_batch_looks_up_siblings_for_its_own_uids(self) -> None:
        asked: list[list[str]] = []
        store = _Store(batches={"all": [dict(self.PENDING_BATCH)]})
        store.sibling_set_names = lambda *, source, uids: asked.append(list(uids)) or []
        _run(store)
        self.assertEqual(asked, [["G9157"]],
                         "resume did not widen from the batch's console_uids")


class ASpentPendingBatchStopsTheRotationTest(unittest.TestCase):
    """A new abandon path, not a relocated one.

    The downloader previously counted attempts and never checked them, so a
    batch that could not be fetched was retried forever -- one vendor slot per
    cycle, account-wide, shared with the tier-3 rotation and the sets backfill.

    The dangerous part is what happens after the skip. That PENDING row still
    holds its 350 console_uids, and the resume-before-claim ordering exists so
    those uids are not booked twice. Skipping it and falling through to
    claim_due_sets would download the same sets again under a second batch.
    So the run stops instead.
    """

    def _spent(self, attempts=5):
        return {"batch_id": "stuck", "status": PENDING, "console_uids": ["G1"],
                "registry_ids": ["r1"], "requested_count": 350,
                "download_attempts": attempts}

    def test_it_does_not_claim_new_sets(self) -> None:
        """The double-booking this guard exists for."""
        store = _Store(batches={"all": [self._spent()]})
        _run(store)
        self.assertEqual(store.inserted, [],
                         "claimed a fresh batch while 350 uids were still booked")

    def test_it_reports_failure_not_a_skip(self) -> None:
        """no_csv_slot means 'next cycle proceeds'. This means 'stopped'.

        Both halves are asserted: the exit code is Render's signal, and
        success=false is what the ledger reads since #224. A mutation flipping
        only the summary passed while the exit code was checked alone.
        """
        import contextlib
        import io
        import json

        store = _Store(batches={"all": [self._spent()]})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = _run(store)
        self.assertEqual(code, 1)
        printed = buffer.getvalue()
        starts = [i for i, line in enumerate(printed.splitlines()) if line.startswith("{")]
        body = "\n".join(printed.splitlines()[starts[-1]:])
        summary = json.loads(body[: body.rindex("}") + 1])
        self.assertIs(summary["success"], False,
                      "a stopped rotation reported success to the ledger")
        self.assertEqual(summary["skippedReason"], "download_attempts_exhausted")

    def test_it_does_not_download(self) -> None:
        store = _Store(batches={"all": [self._spent()]})
        _run(store)
        self.assertEqual([p for _, p in store.updates
                          if p.get("status") == DOWNLOADING], [])

    def test_one_attempt_below_the_limit_still_runs(self) -> None:
        store = _Store(batches={"all": [self._spent(attempts=4)]})
        code = _run(store)
        self.assertEqual(code, 0)
        self.assertTrue([p for _, p in store.updates
                         if p.get("status") == DOWNLOADING])

    def test_a_batch_with_no_download_attempts_runs(self) -> None:
        """Rows predating the split backfill download_attempts to 0."""
        batch = self._spent()
        del batch["download_attempts"]
        store = _Store(batches={"all": [batch]})
        self.assertEqual(_run(store), 0)

    def test_the_limit_is_configurable(self) -> None:
        store = _Store(batches={"all": [self._spent(attempts=6)]})
        self.assertEqual(_run(store, argv=("--commit", "--max-attempts", "9")), 0)
