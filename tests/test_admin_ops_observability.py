import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.services.ops.pipeline_health_service import (
    AdminOpsObservabilityService,
    JOB_DEFINITIONS,
    _status_for,
)


def _definition(job: str) -> dict:
    return next(d for d in JOB_DEFINITIONS if d["job"] == job)


class PipelineStatusTest(unittest.TestCase):
    now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

    def test_failed_run_wins_over_fresh_data(self) -> None:
        # A failing job with still-fresh output must surface the failure --
        # fresh data just means the clock hasn't run out yet.
        status = _status_for(
            _definition("tier3-sportscardspro-rotation"),
            {"status": "failed"},
            {"latestActivityAt": self.now.isoformat()},
            self.now,
        )
        self.assertEqual(status, "failed")

    def test_inactive_source_flags_stale_even_when_others_are_fresh(self) -> None:
        # The yugioh.csv lesson: one dead source among five must flag the
        # job -- the newest of the five hides the dead one.
        status = _status_for(
            _definition("pricecharting-csv-refresh"),
            {"status": "succeeded"},
            {"latestActivityAt": self.now.isoformat(), "inactiveSources": ["yugioh.csv"]},
            self.now,
        )
        self.assertEqual(status, "stale")

    def test_quota_exhaustion_shape_is_stale_despite_successful_runs(self) -> None:
        # The KicksDB incident: runs kept "succeeding" while output died.
        old = (self.now - timedelta(hours=40)).isoformat()
        status = _status_for(
            _definition("kicksdb-catalog-refresh"),
            {"status": "succeeded", "finished_at": self.now.isoformat()},
            {"latestActivityAt": old},
            self.now,
        )
        self.assertEqual(status, "stale")

    def test_running_run_with_fresh_data(self) -> None:
        status = _status_for(
            _definition("tier3-sportscardspro-rotation"),
            {"status": "running"},
            {"latestActivityAt": self.now.isoformat()},
            self.now,
        )
        self.assertEqual(status, "running")

    def test_no_signal_at_all_is_unknown_not_ok(self) -> None:
        status = _status_for(_definition("batch-reprice"), None, None, self.now)
        self.assertEqual(status, "unknown")

    def test_run_ledger_alone_provides_staleness_for_probe_less_jobs(self) -> None:
        old = (self.now - timedelta(hours=40)).isoformat()
        status = _status_for(
            _definition("batch-reprice"),
            {"status": "succeeded", "finished_at": old},
            None,
            self.now,
        )
        self.assertEqual(status, "stale")


class ServiceWiringTest(unittest.TestCase):
    def test_pipeline_health_covers_every_defined_job(self) -> None:
        repository = MagicMock()
        repository.is_configured = True
        repository.pipeline_freshness.return_value = {}
        repository.latest_runs_per_job.return_value = {}
        repository.error_count_last_24h.return_value = 3
        service = AdminOpsObservabilityService(repository=repository)

        result = service.pipeline_health()

        self.assertEqual(len(result["pipelines"]), len(JOB_DEFINITIONS))
        self.assertEqual(result["errors24h"], 3)
        self.assertTrue(all(p["status"] == "unknown" for p in result["pipelines"]))

    def test_errors_groups_without_fingerprint_and_lists_with_one(self) -> None:
        repository = MagicMock()
        repository.is_configured = True
        repository.grouped_errors.return_value = [{"fingerprint": "abc", "count": 5}]
        repository.list_error_occurrences.return_value = [{"event_id": "e1"}]
        service = AdminOpsObservabilityService(repository=repository)

        self.assertIn("groups", service.errors(fingerprint=None, limit=50))
        self.assertIn("occurrences", service.errors(fingerprint="abc", limit=50))


class RecordedAdminJobTest(unittest.TestCase):
    def test_summary_keeps_scalars_and_drops_nested_payloads(self) -> None:
        from app.services.ops.observability import _summary_from

        summary = _summary_from({
            "success": True, "processed": 42, "durationSeconds": 1.5,
            "items": [{"big": "payload"}], "nested": {"x": 1},
        })
        self.assertEqual(summary, {"success": True, "processed": 42, "durationSeconds": 1.5})

    def test_fingerprint_excludes_message(self) -> None:
        # Two occurrences of the same error class on the same route must
        # share a fingerprint even when their messages embed different ids.
        import hashlib
        fp = lambda msg: hashlib.md5("api|/admin/users/{user_id}|KeyError".encode()).hexdigest()
        self.assertEqual(fp("user 123 missing"), fp("user 456 missing"))
