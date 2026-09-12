"""Closing ops_cron_runs rows a suspended job never finished.

Four have sat in 'running' since 2026-09-03. They are not untidiness: every
"is anything unfinished?" check counts them, so a genuinely stuck job looks
exactly like this residue. Naming all four by hand to say "not a new alarm"
was the workaround; this removes the need for it.

The risk being guarded against is the opposite one -- closing a run that is
still working, which would report a live job as failed and hide a real result.
Hence two conditions, never one.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from scripts.close_orphaned_cron_runs import (
    CLOSE_ERROR,
    CLOSE_STATUS,
    DEFAULT_MIN_AGE_HOURS,
    is_orphaned,
    parse_args,
    stale_cutoff,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
CUTOFF = stale_cutoff(min_age_hours=DEFAULT_MIN_AGE_HOURS, now=NOW)


def _run(*, status="running", started=None, finished=None):
    return {"run_id": "r", "job_name": "small-sets-refresh", "status": status,
            "started_at": (started or NOW - timedelta(days=3)).isoformat(),
            "finished_at": finished}


class BothConditionsAreRequiredTest(unittest.TestCase):
    """Age alone would close a job still working. 'running' alone would close
    one that started a minute ago."""

    def test_old_and_running_is_orphaned(self) -> None:
        self.assertTrue(is_orphaned(_run(), cutoff=CUTOFF))

    def test_running_but_recent_is_left_alone(self) -> None:
        self.assertFalse(
            is_orphaned(_run(started=NOW - timedelta(minutes=45)), cutoff=CUTOFF))

    def test_a_long_job_mid_run_is_left_alone(self) -> None:
        """The five-CSV refresh takes ~41 minutes. Six hours cannot reach it."""
        self.assertFalse(
            is_orphaned(_run(started=NOW - timedelta(hours=5)), cutoff=CUTOFF))

    def test_a_finished_row_is_never_touched(self) -> None:
        self.assertFalse(is_orphaned(
            _run(status="succeeded", finished=NOW.isoformat()), cutoff=CUTOFF))

    def test_a_finished_row_is_not_touched_even_if_status_says_running(self) -> None:
        """finished_at is the fact; status could lag it."""
        self.assertFalse(is_orphaned(_run(finished=NOW.isoformat()), cutoff=CUTOFF))

    def test_a_failed_row_is_not_reclosed(self) -> None:
        self.assertFalse(is_orphaned(_run(status="failed"), cutoff=CUTOFF))

    def test_a_row_with_no_start_time_is_left_alone(self) -> None:
        """Nothing to compare against; guessing would risk a live run."""
        row = _run()
        row["started_at"] = None
        self.assertFalse(is_orphaned(row, cutoff=CUTOFF))


class TheFourKnownOrphansTest(unittest.TestCase):
    """The rows this was written for, by their real timestamps.

    Pinned so the selection is demonstrably the one that was reviewed, rather
    than something that happens to match today.
    """

    KNOWN = [
        ("tier3-sportscardspro-rotation", datetime(2026, 9, 3, 10, 13, 17, tzinfo=timezone.utc)),
        ("completed-categories-refresh", datetime(2026, 9, 8, 4, 45, 49, tzinfo=timezone.utc)),
        ("small-sets-refresh", datetime(2026, 9, 8, 7, 41, 0, tzinfo=timezone.utc)),
        ("small-sets-refresh", datetime(2026, 9, 9, 8, 40, 52, tzinfo=timezone.utc)),
    ]

    def test_all_four_are_selected(self) -> None:
        for job, started in self.KNOWN:
            with self.subTest(job=job, started=started):
                row = _run(started=started)
                row["job_name"] = job
                self.assertTrue(is_orphaned(row, cutoff=CUTOFF))

    def test_the_csv_refresh_runs_are_not(self) -> None:
        """They all finished; none must be re-closed. Explicitly excluded by
        request, and by finished_at regardless."""
        for started in (datetime(2026, 9, 11, 14, 30, 27, tzinfo=timezone.utc),
                        datetime(2026, 9, 12, 12, 11, 35, tzinfo=timezone.utc)):
            with self.subTest(started=started):
                row = _run(status="succeeded", started=started,
                           finished=(started + timedelta(minutes=41)).isoformat())
                row["job_name"] = "pricecharting-csv-refresh"
                self.assertFalse(is_orphaned(row, cutoff=CUTOFF))


class HowItIsClosedTest(unittest.TestCase):
    def test_closed_as_failed_not_succeeded(self) -> None:
        """'succeeded' would show work that never completed as work that did."""
        self.assertEqual(CLOSE_STATUS, "failed")

    def test_the_status_is_one_the_table_permits(self) -> None:
        """ops_cron_runs_status_check allows only these three; 'cancelled'
        describes this better but is not available."""
        self.assertIn(CLOSE_STATUS, {"running", "succeeded", "failed"})

    def test_the_error_says_this_was_not_the_job_failing(self) -> None:
        """Otherwise the board shows four failures nobody can diagnose."""
        self.assertIn("suspended or killed", CLOSE_ERROR)
        self.assertIn("not a failure the job itself detected", CLOSE_ERROR)

    def test_nothing_is_deleted(self) -> None:
        import pathlib

        source = pathlib.Path("scripts/close_orphaned_cron_runs.py").read_text().lower()
        for verb in ("delete(", "delete from", ".delete"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, source)

    def test_writing_requires_an_explicit_flag(self) -> None:
        self.assertFalse(parse_args([]).commit)
        self.assertTrue(parse_args(["--commit"]).commit)

    def test_the_threshold_can_be_raised_but_defaults_safe(self) -> None:
        self.assertEqual(parse_args([]).min_age_hours, DEFAULT_MIN_AGE_HOURS)
        self.assertGreaterEqual(DEFAULT_MIN_AGE_HOURS, 1)


class TheUpdateCannotHitALiveRunTest(unittest.TestCase):
    """The PATCH is filtered on status=running as well as the id.

    Between selecting a row and writing it, a job could conceivably finish.
    Without that filter the close would overwrite a real result with 'failed'.
    """

    def test_the_patch_filters_on_running(self) -> None:
        import pathlib

        source = pathlib.Path("scripts/close_orphaned_cron_runs.py").read_text()
        self.assertIn('"status": "eq.running"', source)


if __name__ == "__main__":
    unittest.main()
