import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx

from app.services.data_requests.data_request_service import (
    DELETION_GRACE_PERIOD_DAYS,
    DataRequestError,
    DataRequestNotFoundError,
    DataRequestService,
    DataRequestUnauthorizedError,
    _ALL_DELETION_TABLES,
    _USER_SCOPED_LIST_TABLES,
    _USER_SCOPED_SINGLE_ROW_TABLES,
)

# Export still covers only the user-scoped tables; deletion covers those plus
# support_tickets and user_scan_usage.
_ALL_SCOPED_TABLES = (*_USER_SCOPED_LIST_TABLES, *_USER_SCOPED_SINGLE_ROW_TABLES)


class UserIdFromTokenTest(unittest.TestCase):
    def test_valid_token_resolves_user_id(self) -> None:
        client = _FakeClient()
        service = DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role", anon_key="anon", client=client,
        )
        self.assertEqual(service.user_id_from_token("good-token"), "user-1")

    def test_invalid_token_raises_unauthorized(self) -> None:
        client = _FakeClient()
        service = DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role", anon_key="anon", client=client,
        )
        with self.assertRaises(DataRequestUnauthorizedError):
            service.user_id_from_token("bad-token")

    def test_missing_token_raises_unauthorized(self) -> None:
        client = _FakeClient()
        service = DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role", anon_key="anon", client=client,
        )
        with self.assertRaises(DataRequestUnauthorizedError):
            service.user_id_from_token("")


class CreateAndListRequestsTest(unittest.TestCase):
    def test_create_request_succeeds_when_none_open(self) -> None:
        client = _FakeClient()
        service = DataRequestService(supabase_url="https://supabase.test", service_role_key="role", client=client)

        created = service.create_request(user_id="user-1", request_type="export")

        self.assertEqual(created["type"], "export")
        self.assertEqual(created["status"], "open")
        post = next(r for r in client.requests if r["method"] == "POST" and "data_requests" in r["url"])
        self.assertEqual(post["json"]["user_id"], "user-1")

    def test_create_request_rejects_duplicate_open_request(self) -> None:
        client = _FakeClient(existing_open_request=True)
        service = DataRequestService(supabase_url="https://supabase.test", service_role_key="role", client=client)

        with self.assertRaises(DataRequestError):
            service.create_request(user_id="user-1", request_type="export")

    def test_create_request_rejects_unknown_type(self) -> None:
        client = _FakeClient()
        service = DataRequestService(supabase_url="https://supabase.test", service_role_key="role", client=client)
        with self.assertRaises(DataRequestError):
            service.create_request(user_id="user-1", request_type="bogus")

    def test_list_my_requests_returns_public_shape(self) -> None:
        client = _FakeClient()
        service = DataRequestService(supabase_url="https://supabase.test", service_role_key="role", client=client)

        requests = service.list_my_requests("user-1")

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["userId"], "user-1")

    def test_admin_list_includes_counts(self) -> None:
        client = _FakeClient()
        service = DataRequestService(supabase_url="https://supabase.test", service_role_key="role", client=client)

        payload = service.list_requests()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["openCount"], 2)
        self.assertIsInstance(payload["requests"], list)


class ProcessExportTest(unittest.TestCase):
    def test_dry_run_previews_counts_without_writing_anything(self) -> None:
        client = _FakeClient()
        service = DataRequestService(supabase_url="https://supabase.test", service_role_key="role", client=client)

        result = service.process_request("req-export-1", dry_run=True)

        self.assertTrue(result["dryRun"])
        self.assertIn("preview", result)
        self.assertFalse(any(r["method"] == "PATCH" for r in client.requests))
        self.assertFalse(any("storage" in r["url"] for r in client.requests))

    def test_live_run_uploads_bundle_and_completes_request(self) -> None:
        client = _FakeClient()
        audit = Mock()
        service = DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role", client=client, audit_service=audit,
        )

        result = service.process_request("req-export-1", dry_run=False)

        self.assertFalse(result["dryRun"])
        self.assertIn("downloadUrl", result)
        upload = next(r for r in client.requests if r["method"] == "POST" and "/storage/v1/object/" in r["url"] and "/sign/" not in r["url"])
        self.assertIn("data-exports/user-1/req-export-1.json", upload["url"])
        sign = next(r for r in client.requests if "/storage/v1/object/sign/" in r["url"])
        self.assertIsNotNone(sign)
        patch = next(r for r in client.requests if r["method"] == "PATCH" and "data_requests" in r["url"])
        self.assertEqual(patch["json"]["status"], "completed")
        audit.record.assert_called_once()
        self.assertEqual(audit.record.call_args.kwargs["action"], "data_request.export_processed")

    def test_dry_run_does_not_write_audit_log(self) -> None:
        client = _FakeClient()
        audit = Mock()
        service = DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role", client=client, audit_service=audit,
        )
        service.process_request("req-export-1", dry_run=True)
        audit.record.assert_not_called()


class ProcessDeletionTest(unittest.TestCase):
    def test_dry_run_previews_without_deleting(self) -> None:
        client = _FakeClient()
        service = DataRequestService(supabase_url="https://supabase.test", service_role_key="role", client=client)

        result = service.process_request("req-deletion-1", dry_run=True)

        self.assertIn("preview", result)
        self.assertFalse(any(r["method"] == "DELETE" for r in client.requests))

    def test_live_run_deletes_every_scoped_table_and_the_auth_user(self) -> None:
        client = _FakeClient()
        service = DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role", client=client, audit_service=Mock(),
        )

        result = service.process_request("req-deletion-1", dry_run=False)

        deletes = [r for r in client.requests if r["method"] == "DELETE"]
        deleted_tables = {r["url"].rsplit("/", 1)[-1] for r in deletes if "/rest/v1/" in r["url"]}
        self.assertEqual(deleted_tables, set(_ALL_DELETION_TABLES))
        auth_delete = [r for r in deletes if "/auth/v1/admin/users/" in r["url"]]
        self.assertEqual(len(auth_delete), 1)
        self.assertTrue(result["receipt"]["authAccountDeleted"])

    def test_deletion_never_touches_admin_audit_events(self) -> None:
        client = _FakeClient()
        service = DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role", client=client, audit_service=Mock(),
        )

        service.process_request("req-deletion-1", dry_run=False)

        self.assertFalse(any("admin_audit_events" in r["url"] for r in client.requests))

    def test_live_run_marks_request_completed_with_receipt(self) -> None:
        client = _FakeClient()
        service = DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role", client=client, audit_service=Mock(),
        )

        service.process_request("req-deletion-1", dry_run=False)

        patch = next(r for r in client.requests if r["method"] == "PATCH" and "data_requests" in r["url"])
        self.assertEqual(patch["json"]["status"], "completed")
        self.assertTrue(patch["json"]["raw_json"]["auditEventsExcluded"])


class ProcessRequestErrorsTest(unittest.TestCase):
    def test_unknown_request_id_raises_not_found(self) -> None:
        client = _FakeClient()
        service = DataRequestService(supabase_url="https://supabase.test", service_role_key="role", client=client)
        with self.assertRaises(DataRequestNotFoundError):
            service.process_request("does-not-exist", dry_run=True)


class ScheduleDeletionTest(unittest.TestCase):
    def _service(self, client):
        return DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role",
            anon_key="anon", client=client, audit_service=Mock(),
        )

    def test_schedule_writes_a_scheduled_row_with_a_future_date(self) -> None:
        client = _FakeClient()
        result = self._service(client).schedule_deletion(user_id="user-1")

        # The app reads scheduledFor off this response to show the date, so a
        # missing mapping here silently degrades to "no date shown".
        self.assertIn("scheduledFor", result)
        self.assertIsNotNone(result["scheduledFor"])

        posts = [
            r for r in client.requests
            if r["method"] == "POST" and "/rest/v1/data_requests" in r["url"]
        ]
        self.assertEqual(len(posts), 1)
        payload = posts[0]["json"]
        self.assertEqual(payload["status"], "scheduled")
        self.assertEqual(payload["type"], "deletion")
        scheduled_for = datetime.fromisoformat(payload["scheduled_for"])
        delta_days = (scheduled_for - datetime.now(timezone.utc)).days
        # 29 rather than 30 because the subtraction truncates.
        self.assertEqual(delta_days, DELETION_GRACE_PERIOD_DAYS - 1)

    def test_schedule_deletes_nothing_immediately(self) -> None:
        """The whole point of the grace period: confirming is not destructive."""
        client = _FakeClient()
        self._service(client).schedule_deletion(user_id="user-1")

        self.assertEqual([r for r in client.requests if r["method"] == "DELETE"], [])
        self.assertEqual(client.deleted_storage_prefixes, [])

    def test_scheduling_twice_does_not_move_the_date(self) -> None:
        existing = {
            "id": "req-1", "user_id": "user-1", "type": "deletion",
            "status": "scheduled", "scheduled_for": "2026-10-01T00:00:00+00:00",
        }
        client = _FakeClient(scheduled_deletion=existing)
        result = self._service(client).schedule_deletion(user_id="user-1")

        self.assertEqual(result["id"], "req-1")
        self.assertEqual(
            [r for r in client.requests
             if r["method"] == "POST" and "/rest/v1/data_requests" in r["url"]],
            [],
        )


class CancelDeletionTest(unittest.TestCase):
    def _service(self, client):
        return DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role",
            anon_key="anon", client=client, audit_service=Mock(),
        )

    def test_cancel_marks_the_row_cancelled(self) -> None:
        existing = {
            "id": "req-1", "user_id": "user-1", "type": "deletion",
            "status": "scheduled", "scheduled_for": "2026-10-01T00:00:00+00:00",
        }
        client = _FakeClient(scheduled_deletion=existing)
        self._service(client).cancel_deletion(user_id="user-1")

        patches = [
            r for r in client.requests
            if r["method"] == "PATCH" and "/rest/v1/data_requests" in r["url"]
        ]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["json"]["status"], "cancelled")
        self.assertIsNotNone(patches[0]["json"]["cancelled_at"])
        self.assertEqual(patches[0]["params"]["id"], "eq.req-1")

    def test_cancel_without_a_scheduled_deletion_raises_not_found(self) -> None:
        client = _FakeClient(scheduled_deletion=None)
        with self.assertRaises(DataRequestNotFoundError):
            self._service(client).cancel_deletion(user_id="user-1")

    def test_status_reports_scheduled_state(self) -> None:
        existing = {
            "id": "req-1", "user_id": "user-1", "type": "deletion",
            "status": "scheduled", "scheduled_for": "2026-10-01T00:00:00+00:00",
        }
        service = self._service(_FakeClient(scheduled_deletion=existing))
        self.assertEqual(
            service.deletion_status("user-1"),
            {"scheduled": True, "scheduledFor": "2026-10-01T00:00:00+00:00", "requestId": "req-1"},
        )

    def test_status_reports_nothing_scheduled(self) -> None:
        service = self._service(_FakeClient(scheduled_deletion=None))
        self.assertEqual(
            service.deletion_status("user-1"),
            {"scheduled": False, "scheduledFor": None, "requestId": None},
        )


class PurgeDueTest(unittest.TestCase):
    def _service(self, client):
        return DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role",
            anon_key="anon", client=client, audit_service=Mock(),
        )

    def test_dry_run_previews_without_deleting(self) -> None:
        due = [{"id": "req-1", "user_id": "user-1", "type": "deletion", "status": "scheduled"}]
        client = _FakeClient(due_deletions=due)
        result = self._service(client).purge_due(dry_run=True)

        self.assertEqual(result["dueCount"], 1)
        self.assertEqual(result["processedCount"], 1)
        self.assertEqual([r for r in client.requests if r["method"] == "DELETE"], [])

    def test_live_run_deletes_every_table_and_the_auth_user(self) -> None:
        due = [{"id": "req-1", "user_id": "user-1", "type": "deletion", "status": "scheduled"}]
        client = _FakeClient(due_deletions=due)
        result = self._service(client).purge_due(dry_run=False)

        self.assertEqual(result["failedCount"], 0)
        deletes = [r for r in client.requests if r["method"] == "DELETE"]
        deleted_tables = {r["url"].rsplit("/", 1)[-1] for r in deletes if "/rest/v1/" in r["url"]}
        self.assertEqual(deleted_tables, set(_ALL_DELETION_TABLES))
        self.assertTrue(any("/auth/v1/admin/users/" in r["url"] for r in deletes))

    def test_nothing_due_is_a_clean_no_op(self) -> None:
        client = _FakeClient(due_deletions=[])
        result = self._service(client).purge_due(dry_run=False)

        self.assertEqual(result["dueCount"], 0)
        self.assertEqual([r for r in client.requests if r["method"] == "DELETE"], [])

    def test_one_failing_account_does_not_stop_the_others(self) -> None:
        due = [
            {"id": "req-bad", "user_id": "user-bad", "type": "deletion", "status": "scheduled"},
            {"id": "req-ok", "user_id": "user-1", "type": "deletion", "status": "scheduled"},
        ]
        client = _FailOnUserClient(failing_user="user-bad", due_deletions=due)
        result = self._service(client).purge_due(dry_run=False)

        self.assertEqual(result["dueCount"], 2)
        self.assertEqual(result["processedCount"], 1)
        self.assertEqual(result["failedCount"], 1)
        self.assertEqual(result["failed"][0]["requestId"], "req-bad")


class DeletionStorageTest(unittest.TestCase):
    def _service(self, client):
        return DataRequestService(
            supabase_url="https://supabase.test", service_role_key="role",
            anon_key="anon", client=client, audit_service=Mock(),
        )

    def test_walks_nested_folders_and_deletes_every_object(self) -> None:
        """Gallery variants nest a level below the primary image."""
        listing = {
            "users/user-1/": [
                {"name": "portfolio_images", "id": None},
                {"name": "profile", "id": None},
            ],
            "users/user-1/portfolio_images/": [
                {"name": "item-a.jpg", "id": "1"},
                {"name": "item-b", "id": None},
            ],
            "users/user-1/portfolio_images/item-b/": [
                {"name": "00-front.jpg", "id": "2"},
                {"name": "01-back.jpg", "id": "3"},
            ],
            "users/user-1/profile/": [{"name": "avatar.jpg", "id": "4"}],
            "data-exports/user-1/": [{"name": "bundle.json", "id": "5"}],
        }
        client = _FakeClient(
            due_deletions=[{"id": "req-1", "user_id": "user-1", "type": "deletion", "status": "scheduled"}],
            storage_listing=listing,
            attachment_paths=["support-attachments/msg-1/screenshot.png"],
        )
        self._service(client).purge_due(dry_run=False)

        self.assertEqual(
            set(client.deleted_storage_prefixes),
            {
                "users/user-1/portfolio_images/item-a.jpg",
                "users/user-1/portfolio_images/item-b/00-front.jpg",
                "users/user-1/portfolio_images/item-b/01-back.jpg",
                "users/user-1/profile/avatar.jpg",
                "data-exports/user-1/bundle.json",
                "support-attachments/msg-1/screenshot.png",
            },
        )

    def test_dry_run_counts_storage_without_deleting_it(self) -> None:
        listing = {"users/user-1/": [{"name": "a.jpg", "id": "1"}, {"name": "b.jpg", "id": "2"}]}
        client = _FakeClient(
            due_deletions=[{"id": "req-1", "user_id": "user-1", "type": "deletion", "status": "scheduled"}],
            storage_listing=listing,
        )
        result = self._service(client).purge_due(dry_run=True)

        self.assertEqual(result["processed"][0]["preview"]["storageObjectCount"], 2)
        self.assertEqual(client.deleted_storage_prefixes, [])

    def test_storage_failure_does_not_abort_the_account_deletion(self) -> None:
        """An orphaned image beats an account that never finishes deleting."""
        listing = {"users/user-1/": [{"name": "a.jpg", "id": "1"}]}
        client = _StorageDeleteFailsClient(
            due_deletions=[{"id": "req-1", "user_id": "user-1", "type": "deletion", "status": "scheduled"}],
            storage_listing=listing,
        )
        result = self._service(client).purge_due(dry_run=False)

        self.assertEqual(result["failedCount"], 0)
        self.assertEqual(result["processed"][0]["receipt"]["storageObjectsDeleted"], 0)
        self.assertTrue(
            any("/auth/v1/admin/users/" in r["url"] for r in client.requests if r["method"] == "DELETE")
        )


class _FakeClient:
    def __init__(
        self,
        *,
        existing_open_request: bool = False,
        scheduled_deletion: dict | None = None,
        due_deletions: list[dict] | None = None,
        storage_listing: dict[str, list[dict]] | None = None,
        attachment_paths: list[str] | None = None,
    ) -> None:
        self.requests: list[dict] = []
        self._existing_open_request = existing_open_request
        self._scheduled_deletion = scheduled_deletion
        self._due_deletions = due_deletions or []
        # prefix (with trailing slash) -> list of Storage list() entries.
        # Folders are entries whose id is None, matching Supabase's shape.
        self._storage_listing = storage_listing or {}
        self._attachment_paths = attachment_paths or []
        self.deleted_storage_prefixes: list[str] = []

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        params = kwargs.get("params") or {}

        if url.endswith("/auth/v1/user"):
            token = kwargs["headers"]["Authorization"].split(" ", 1)[1]
            if token == "good-token":
                return _response({"id": "user-1"})
            return _response({}, status_code=401)

        if "/auth/v1/admin/users/" in url and method == "DELETE":
            return _response(None)

        if "/storage/v1/object/list/" in url and method == "POST":
            prefix = (kwargs.get("json") or {}).get("prefix", "")
            offset = (kwargs.get("json") or {}).get("offset", 0)
            # Single page per prefix is enough for these tests.
            return _response(self._storage_listing.get(prefix, []) if offset == 0 else [])

        if "/storage/v1/object/sign/" in url:
            return _response({"signedURL": "/object/sign/bucket/path?token=abc"})

        if "/storage/v1/object/" in url and method == "POST":
            return _response({"Key": "uploaded"})

        if "/storage/v1/object/" in url and method == "DELETE":
            self.deleted_storage_prefixes.extend((kwargs.get("json") or {}).get("prefixes", []))
            return _response(None)

        if "/rest/v1/data_requests" in url:
            if method == "POST":
                # PostgREST echoes back the row it inserted, defaults and all,
                # so the fake must too -- the app reads scheduledFor off this.
                payload = kwargs["json"]
                return _response([{"id": "new-req", "status": "open", **payload}])
            if method == "PATCH":
                # PostgREST echoes the row back only when asked to; the
                # deletion receipt PATCH uses return=minimal and gets nothing.
                if kwargs.get("headers", {}).get("Prefer") == "return=representation":
                    return _response([{
                        "id": (params.get("id") or "eq.req-1").removeprefix("eq."),
                        "user_id": "user-1",
                        "type": "deletion",
                        **(kwargs.get("json") or {}),
                    }])
                return _response(None)
            if method == "GET":
                # purge cron: scheduled rows whose window has elapsed
                if params.get("status") == "eq.scheduled" and "scheduled_for" in params:
                    return _response(self._due_deletions)
                # the app's scheduled-deletion lookup
                if params.get("status") == "eq.scheduled":
                    return _response([self._scheduled_deletion] if self._scheduled_deletion else [])
                # existing-open-request duplicate check
                if params.get("status") == "in.(open,processing)":
                    return _response([{"id": "existing"}] if self._existing_open_request else [])
                if params.get("id") == "eq.req-export-1":
                    return _response([{"id": "req-export-1", "user_id": "user-1", "type": "export", "status": "open"}])
                if params.get("id") == "eq.req-deletion-1":
                    return _response([{"id": "req-deletion-1", "user_id": "user-1", "type": "deletion", "status": "open"}])
                if params.get("id") == "eq.does-not-exist":
                    return _response([])
                if params.get("status") == "eq.completed" and "requested_at,completed_at" in params.get("select", ""):
                    return _response(
                        [{"requested_at": "2026-08-01T00:00:00Z", "completed_at": "2026-08-03T00:00:00Z"}]
                    )
                if kwargs.get("headers", {}).get("Prefer") == "count=exact":
                    return _response([], headers={"content-range": "0-0/2"})
                if params.get("user_id") == "eq.user-1" and "order" in params:
                    return _response([{"id": "r1", "user_id": "user-1", "type": "export", "status": "open"}])
                return _response([{"id": "r1", "user_id": "user-1", "type": "export", "status": "open"}])

        if "/rest/v1/" in url:
            if method == "DELETE":
                return _response(None)
            if method == "GET":
                if "support_message_attachments" in url:
                    return _response([{"file_path": path} for path in self._attachment_paths])
                return _response([{"id": "row-1", "user_id": params.get("user_id", "").removeprefix("eq.")}])

        raise AssertionError(f"Unexpected request: {method} {url} params={params}")


class _FailOnUserClient(_FakeClient):
    """Blows up on one specific user, to prove the purge loop isolates failures."""

    def __init__(self, *, failing_user: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._failing_user = failing_user

    def request(self, method: str, url: str, **kwargs):
        params = kwargs.get("params") or {}
        if params.get("user_id") == f"eq.{self._failing_user}":
            raise httpx.ConnectError("boom")
        return super().request(method, url, **kwargs)


class _StorageDeleteFailsClient(_FakeClient):
    """Storage delete fails; everything else succeeds."""

    def request(self, method: str, url: str, **kwargs):
        if "/storage/v1/object/" in url and method == "DELETE":
            raise httpx.ConnectError("storage down")
        return super().request(method, url, **kwargs)


def _response(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=payload,
        headers=headers,
        request=httpx.Request("GET", "https://example.test"),
    )


if __name__ == "__main__":
    unittest.main()
