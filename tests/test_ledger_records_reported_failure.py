"""A run that reports failure must not sit in the ledger as green.

Four rows did. Two sports downloads refused over a duplicate console_uid
(2026-09-09) and two failed ingests (2026-09-08) all carried
summary.success = false and status = 'succeeded'.

The cause was in run_with_recorder, not in the scripts: finish_succeeded() was
called for every non-exception return, and the exit code reached the row only
as summary.exitCode. The summary's own success flag was never consulted.

That also means #217's `return 1` on the CSV refresh never flipped a row --
the fix was real but the signal stopped one layer short, which is precisely
the failure mode #217 existed to remove. Fixing it here fixes every script at
once rather than one return statement at a time.

The exit code and the ledger status answer different questions and are kept
apart deliberately: the code is Render's alerting signal, where a vendor 503
is quiet backpressure rather than an incident; the status is whether the work
happened, and a batch that never downloaded did not happen however calmly it
declined.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import scripts._ops_run_recorder as recorder_module
from scripts._ops_run_recorder import _failed


class WhatCountsAsFailedTest(unittest.TestCase):
    def test_an_explicit_success_false_is_failure(self) -> None:
        self.assertTrue(_failed({"success": False}, 0))

    def test_it_wins_even_when_the_script_exits_zero(self) -> None:
        """The whole bug: download_catalog_batch returns 0 after refusing a
        batch, because a refusal is not a crash."""
        self.assertTrue(_failed({"success": False, "status": "validation_failed"}, 0))

    def test_a_non_zero_exit_is_failure_without_any_summary(self) -> None:
        """Fallback for scripts that report no summary at all."""
        self.assertTrue(_failed(None, 1))

    def test_a_clean_run_is_not_failure(self) -> None:
        self.assertFalse(_failed({"success": True}, 0))

    def test_a_summary_with_no_success_key_is_not_failure(self) -> None:
        """Absence is not denial -- several scripts report no such field."""
        self.assertFalse(_failed({"rows": 5}, 0))

    def test_success_true_with_a_non_zero_exit_is_still_failure(self) -> None:
        """Contradictory, so take the worse reading rather than the kinder."""
        self.assertTrue(_failed({"success": True}, 1))

    def test_a_skip_that_reports_success_is_not_failure(self) -> None:
        """no_due_sets and queue_full are skips, not failures."""
        self.assertFalse(_failed({"success": True, "skippedReason": "no_due_sets"}, 0))


class TheLedgerRowGetsTheRightStatusTest(unittest.TestCase):
    def _run(self, summary, exit_code):
        calls: list[tuple] = []

        class _Recorder:
            def start(self_inner): pass

            def finish_succeeded(self_inner, code):
                calls.append(("succeeded", code, None))

            def finish_reported_failure(self_inner, code, *, summary):
                calls.append(("failed", code, summary))

            def finish_failed(self_inner, error):
                calls.append(("failed-exception", None, None))

        with patch.object(recorder_module, "_Recorder", lambda name: _Recorder()):
            recorder_module._summary = summary
            try:
                recorder_module.run_with_recorder("job", lambda: exit_code)
            finally:
                recorder_module._summary = None
        return calls

    def test_a_refused_batch_is_recorded_failed(self) -> None:
        calls = self._run({"success": False, "status": "validation_failed"}, 0)
        self.assertEqual(calls[0][0], "failed")

    def test_a_clean_run_is_recorded_succeeded(self) -> None:
        self.assertEqual(self._run({"success": True}, 0)[0][0], "succeeded")

    def test_the_summary_travels_with_the_failure(self) -> None:
        """Otherwise the row says failed and cannot say why."""
        calls = self._run({"success": False, "skippedReason": "no_csv_slot"}, 0)
        self.assertEqual(calls[0][2]["skippedReason"], "no_csv_slot")

    def test_the_exit_code_is_still_returned_unchanged(self) -> None:
        """Render's alerting must not change because the ledger got stricter --
        a vendor 503 stays a quiet zero."""
        with patch.object(recorder_module, "_Recorder",
                          lambda name: type("R", (), {
                              "start": lambda s: None,
                              "finish_succeeded": lambda s, c: None,
                              "finish_reported_failure": lambda s, c, summary: None,
                              "finish_failed": lambda s, e: None})()):
            recorder_module._summary = {"success": False}
            try:
                code = recorder_module.run_with_recorder("job", lambda: 0)
            finally:
                recorder_module._summary = None
        self.assertEqual(code, 0)


class AReportedFailureIsNotAnErrorEventTest(unittest.TestCase):
    """finish_reported_failure writes no ops_error_events row.

    There is no traceback to fingerprint and the script has already described
    itself. Writing one would put a stackless entry into the error feed for
    every quiet vendor 503.
    """

    def test_it_does_not_record_an_error_event(self) -> None:
        import inspect

        source = inspect.getsource(recorder_module._Recorder.finish_reported_failure)
        self.assertNotIn("_record_error_event", source)

    def test_an_exception_still_does(self) -> None:
        import inspect

        source = inspect.getsource(recorder_module._Recorder.finish_failed)
        self.assertIn("_record_error_event", source)


if __name__ == "__main__":
    unittest.main()


class TheRowItselfCarriesTheSummaryTest(unittest.TestCase):
    """Asserted against _Recorder, not a stand-in for it.

    An earlier version of these tests replaced _Recorder wholesale, so a
    mutation dropping the summary on the way into _finish passed: the test
    only ever saw what was handed to the fake. A row that says 'failed' and
    cannot say why sends the reader to Render's rotating logs, which is what
    the ledger exists to avoid.
    """

    def _finish_call(self, summary, exit_code=0):
        captured: dict = {}
        recorder = recorder_module._Recorder("job")
        with patch.object(recorder, "_finish",
                          lambda status, **kw: captured.update(status=status, **kw)):
            recorder.finish_reported_failure(exit_code, summary=summary)
        return captured

    def test_the_summary_reaches_finish(self) -> None:
        call = self._finish_call({"success": False, "skippedReason": "no_csv_slot"})
        self.assertEqual(call["summary"]["skippedReason"], "no_csv_slot")

    def test_the_status_is_failed(self) -> None:
        self.assertEqual(self._finish_call({"success": False})["status"], "failed")

    def test_the_reason_names_the_skip(self) -> None:
        """'success=false' alone does not say which of several exits it was."""
        call = self._finish_call({"success": False, "skippedReason": "no_csv_slot"})
        self.assertIn("no_csv_slot", call["error"])

    def test_a_non_zero_exit_is_named_too(self) -> None:
        call = self._finish_call({"success": False}, exit_code=1)
        self.assertIn("exit code 1", call["error"])

    def test_the_exit_code_reaches_the_row(self) -> None:
        self.assertEqual(self._finish_call({"success": False}, exit_code=1)["exit_code"], 1)
