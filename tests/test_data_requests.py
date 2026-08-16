import unittest
from unittest.mock import Mock

import httpx

from app.services.data_requests.data_request_service import (
    DataRequestError,
    DataRequestNotFoundError,
    DataRequestService,
    DataRequestUnauthorizedError,
    _USER_SCOPED_LIST_TABLES,
    _USER_SCOPED_SINGLE_ROW_TABLES,
)

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
        self.assertEqual(deleted_tables, set(_ALL_SCOPED_TABLES))
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


class _FakeClient:
    def __init__(self, *, existing_open_request: bool = False) -> None:
        self.requests: list[dict] = []
        self._existing_open_request = existing_open_request

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

        if "/storage/v1/object/sign/" in url:
            return _response({"signedURL": "/object/sign/bucket/path?token=abc"})

        if "/storage/v1/object/" in url and method == "POST":
            return _response({"Key": "uploaded"})

        if "/rest/v1/data_requests" in url:
            if method == "POST":
                return _response(
                    [{"id": "new-req", "user_id": kwargs["json"]["user_id"], "type": kwargs["json"]["type"], "status": "open"}]
                )
            if method == "PATCH":
                return _response(None)
            if method == "GET":
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
                return _response([{"id": "row-1", "user_id": params.get("user_id", "").removeprefix("eq.")}])

        raise AssertionError(f"Unexpected request: {method} {url} params={params}")


def _response(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=payload,
        headers=headers,
        request=httpx.Request("GET", "https://example.test"),
    )


if __name__ == "__main__":
    unittest.main()
