"""Postgres statement timeouts must reach the error board.

The daily completed-categories run was losing ~60% of its batches to
SQLSTATE 57014 while reporting success, and ops_error_events contained zero
matching records across fourteen days. The failure path is

    _upsert -> SystemExit -> PartialCatalogWriteError -> write_catalog_rows -> False

which prints and returns, so nothing was ever recorded. These tests pin the
recording, its diagnostic content, and -- just as importantly -- that it
stays quiet for everything else.
"""

import json
import re
import unittest
from unittest import mock

import httpx

from scripts import _ops_run_recorder as recorder_module
from scripts._ops_run_recorder import (
    SQLSTATE_STATEMENT_TIMEOUT,
    build_db_failure_event,
    record_db_timeout,
    sqlstate_of,
)
from scripts.import_pricecharting_catalog import SupabaseCatalogClient

TIMEOUT_BODY = json.dumps({
    "code": "57014",
    "details": None,
    "hint": None,
    "message": "canceling statement due to statement timeout",
})
TOKEN = "super-secret-token"


class _FakeRecorder:
    """Stands in for the in-flight run, capturing what would be POSTed."""

    job_name = "completed-categories-refresh"
    run_id = "run-123"
    _configured = True

    def __init__(self):
        self.posts = []

    def _request(self, method, path, payload, *, headers):
        self.posts.append((path, payload))
        return None


class SqlstateDetectionTest(unittest.TestCase):
    def test_reads_the_code_field_rather_than_matching_prose(self):
        self.assertEqual(sqlstate_of(TIMEOUT_BODY), SQLSTATE_STATEMENT_TIMEOUT)

    def test_falls_back_to_message_when_the_body_is_not_json(self):
        self.assertEqual(
            sqlstate_of("canceling statement due to statement timeout"),
            SQLSTATE_STATEMENT_TIMEOUT,
        )

    def test_other_sqlstates_are_reported_as_themselves(self):
        self.assertEqual(sqlstate_of('{"code":"23514"}'), "23514")

    def test_unparseable_bodies_yield_nothing(self):
        self.assertIsNone(sqlstate_of("<html>502 Bad Gateway</html>"))
        self.assertIsNone(sqlstate_of(""))


class EventContentTest(unittest.TestCase):
    def _event(self, **kw):
        base = dict(
            job_name="completed-categories-refresh", run_id="run-123",
            operation="catalog_upsert", row_count=39,
            sqlstate=SQLSTATE_STATEMENT_TIMEOUT, status_code=500,
            body=TIMEOUT_BODY, context={"table": "pricecharting_catalog"},
        )
        base.update(kw)
        return build_db_failure_event(**base)

    def test_carries_the_diagnostics_needed_to_correlate_contention(self):
        ctx = self._event()["context"]
        self.assertEqual(ctx["operation"], "catalog_upsert")
        self.assertEqual(ctx["rowCount"], 39)
        self.assertEqual(ctx["sqlstate"], SQLSTATE_STATEMENT_TIMEOUT)
        self.assertEqual(ctx["runId"], "run-123")
        self.assertEqual(ctx["table"], "pricecharting_catalog")

    def test_groups_by_job_and_operation_so_the_board_does_not_fragment(self):
        same = self._event(row_count=12)["fingerprint"]
        self.assertEqual(self._event(row_count=99)["fingerprint"], same)
        self.assertNotEqual(self._event(operation="history_insert")["fingerprint"], same)

    def test_credentials_in_the_body_are_scrubbed(self):
        leaky = f'{{"code":"57014","details":"https://x.test/a?t={TOKEN}&b=1"}}'
        event = self._event(body=leaky)
        self.assertNotIn(TOKEN, json.dumps(event))
        self.assertIn("[REDACTED]", event["message"])

    def test_no_catalogue_payload_is_carried(self):
        event = self._event()
        self.assertNotIn("rows", event["context"])
        self.assertNotIn("payload", event["context"])


class RecordingTest(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeRecorder()
        patcher = mock.patch.object(recorder_module, "_active", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self, body, **kw):
        return record_db_timeout(
            operation=kw.pop("operation", "catalog_upsert"),
            row_count=kw.pop("row_count", 39),
            status_code=kw.pop("status_code", 500),
            body=body, **kw,
        )

    def test_a_timeout_writes_exactly_one_event(self):
        self.assertTrue(self._record(TIMEOUT_BODY))
        self.assertEqual(len(self.fake.posts), 1)
        path, payload = self.fake.posts[0]
        self.assertEqual(path, "/rest/v1/ops_error_events")
        self.assertEqual(payload["error_class"], "PostgresError57014")

    def test_other_database_errors_are_not_recorded(self):
        """23514 is the SCD2 valid-window violation -- a real bug, but not
        the failure this hook exists to diagnose. Recording everything would
        make the board useless."""
        self.assertFalse(self._record('{"code":"23514"}'))
        self.assertEqual(self.fake.posts, [])

    def test_successful_writes_record_nothing(self):
        self.assertFalse(self._record(""))
        self.assertEqual(self.fake.posts, [])

    def test_recording_never_raises_even_if_the_board_is_down(self):
        self.fake._request = mock.Mock(side_effect=httpx.ConnectError("down"))
        self.assertFalse(self._record(TIMEOUT_BODY))

    def test_no_event_when_no_run_is_in_flight(self):
        with mock.patch.object(recorder_module, "_active", None):
            self.assertFalse(self._record(TIMEOUT_BODY))


class WritePathIntegrationTest(unittest.TestCase):
    """Each DB write helper must report its own operation and row count."""

    def setUp(self):
        self.fake = _FakeRecorder()
        patcher = mock.patch.object(recorder_module, "_active", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = SupabaseCatalogClient(
            supabase_url="https://db.test", service_role_key="k", timeout_seconds=5
        )

    def _timeout_transport(self):
        return httpx.MockTransport(
            lambda request: httpx.Response(500, text=TIMEOUT_BODY)
        )

    def _rows(self, n):
        return [
            {"pricecharting_id": str(i), "product_name": f"Card {i}",
             "content_hash": f"h{i}", "change_hash": f"c{i}", "currency": "USD"}
            for i in range(n)
        ]

    def test_catalog_upsert_timeout_names_its_operation_and_row_count(self):
        with mock.patch(
            "httpx.Client",
            return_value=httpx.Client(transport=self._timeout_transport()),
        ):
            with self.assertRaises(SystemExit):
                self.client._upsert(
                    table="pricecharting_catalog", rows=self._rows(39),
                    batch_size=40, on_conflict="pricecharting_id", label="catalog",
                )
        self.assertEqual(len(self.fake.posts), 1)
        ctx = self.fake.posts[0][1]["context"]
        self.assertEqual(ctx["operation"], "catalog_upsert")
        self.assertEqual(ctx["rowCount"], 39)
        self.assertEqual(ctx["writeBatchSize"], 40)

    def test_history_insert_timeout_is_attributed_separately(self):
        with httpx.Client(transport=self._timeout_transport()) as client:
            with self.assertRaises(SystemExit):
                self.client._insert_history_rows(client, self._rows(40), batch_offset=0)
        self.assertEqual(len(self.fake.posts), 1)
        self.assertEqual(self.fake.posts[0][1]["context"]["operation"], "history_insert")
        self.assertEqual(self.fake.posts[0][1]["context"]["rowCount"], 40)

    def test_history_close_timeout_is_attributed_separately(self):
        with httpx.Client(transport=self._timeout_transport()) as client:
            with self.assertRaises(SystemExit):
                self.client._close_current_history_rows(
                    client, pricecharting_ids=["1", "2", "3"], valid_to="2026-01-01T00:00:00Z"
                )
        self.assertEqual(len(self.fake.posts), 1)
        self.assertEqual(self.fake.posts[0][1]["context"]["operation"], "history_close")
        self.assertEqual(self.fake.posts[0][1]["context"]["rowCount"], 3)


class LayerDeduplicationTest(unittest.TestCase):
    """One failed request must produce one event, not one per layer.

    The exception crosses three of them -- _upsert raises SystemExit,
    upsert_rows converts it to PartialCatalogWriteError, write_catalog_rows
    catches that and returns False. Recording at the outermost layer would
    have been easier and would have lost the row count and operation;
    recording at more than one would flood the board."""

    def setUp(self):
        self.fake = _FakeRecorder()
        patcher = mock.patch.object(recorder_module, "_active", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_timeout_crossing_three_layers_records_once(self):
        from scripts.import_pricecharting_catalog import PartialCatalogWriteError

        client = SupabaseCatalogClient(
            supabase_url="https://db.test", service_role_key="k", timeout_seconds=5
        )

        def handler(request):
            # The change-detection GET succeeds and reports nothing stored,
            # so every row counts as changed and reaches the upsert.
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(500, text=TIMEOUT_BODY)

        rows = [
            {"pricecharting_id": str(i), "product_name": f"Card {i}",
             "content_hash": f"h{i}", "currency": "USD"}
            for i in range(20)
        ]
        # A fresh client per call: upsert_rows and _upsert each open one with
        # `with`, and httpx refuses to reopen a single instance. The real
        # class is captured first, or the factory re-enters the patch.
        real_client = httpx.Client
        with mock.patch(
            "httpx.Client",
            side_effect=lambda *a, **k: real_client(
                transport=httpx.MockTransport(handler)
            ),
        ):
            with self.assertRaises((SystemExit, PartialCatalogWriteError)):
                client.upsert_rows(rows, batch_size=40)

        self.assertEqual(
            len(self.fake.posts), 1,
            f"expected exactly one event, got {len(self.fake.posts)}",
        )


class ProductionConfigTest(unittest.TestCase):
    def test_completed_categories_writes_at_the_validated_size(self):
        """40 is the size measured to write cleanly against this table; 150
        still lost ~15% of writes and 500 failed outright."""
        blueprint = open("render.yaml").read()
        command = next(
            line for line in blueprint.splitlines()
            if "scripts.refresh_completed_pricecharting_categories" in line
        )
        match = re.search(r"--catalog-batch-size (\d+)", command)
        self.assertIsNotNone(match, command)
        self.assertEqual(int(match.group(1)), 40)

    def test_the_provider_side_of_that_job_is_untouched(self):
        blueprint = open("render.yaml").read()
        command = next(
            line for line in blueprint.splitlines()
            if "scripts.refresh_completed_pricecharting_categories" in line
        )
        self.assertIn("--batch-size 300", command)
        self.assertIn("--sleep-between-requests-seconds 600", command)


if __name__ == "__main__":
    unittest.main()
