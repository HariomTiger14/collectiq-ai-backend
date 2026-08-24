import unittest
from unittest.mock import Mock

import httpx

from app.services.support.support_ticket_service import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    SupportTicketError,
    SupportTicketNotFoundError,
    SupportTicketService,
    SupportTicketUnauthorizedError,
)


def _service(client, **kwargs):
    return SupportTicketService(
        supabase_url="https://supabase.test",
        service_role_key="service-role",
        anon_key="anon",
        client=client,
        push_service=kwargs.pop("push_service", Mock()),
        email_service=kwargs.pop("email_service", Mock(is_configured=True)),
        user_repository=kwargs.pop("user_repository", Mock()),
        **kwargs,
    )


class CreateTicketTest(unittest.TestCase):
    def test_creates_ticket_and_first_message(self) -> None:
        client = _FakeClient()
        svc = _service(client)

        result = svc.create_ticket(
            user_id="user-1", category="bug", subject="App crashed", message="It crashed on launch.",
        )

        self.assertEqual(result["category"], "bug")
        self.assertEqual(result["lastMessageId"], "message-1")
        ticket_post = next(r for r in client.requests if r["method"] == "POST" and r["url"].endswith("support_tickets"))
        self.assertEqual(ticket_post["json"]["category"], "bug")
        message_post = next(r for r in client.requests if r["method"] == "POST" and r["url"].endswith("support_messages"))
        self.assertEqual(message_post["json"]["sender_type"], "user")

    def test_rejects_unknown_category(self) -> None:
        svc = _service(_FakeClient())
        with self.assertRaises(SupportTicketError):
            svc.create_ticket(user_id="user-1", category="nonsense", subject="x", message="y")

    def test_rejects_empty_subject_or_message(self) -> None:
        svc = _service(_FakeClient())
        with self.assertRaises(SupportTicketError):
            svc.create_ticket(user_id="user-1", category="bug", subject="", message="y")
        with self.assertRaises(SupportTicketError):
            svc.create_ticket(user_id="user-1", category="bug", subject="x", message="")


class TicketThreadTest(unittest.TestCase):
    def test_owner_can_view_their_own_ticket(self) -> None:
        client = _FakeClient()
        svc = _service(client)

        result = svc.get_ticket_thread(ticket_id="ticket-1", user_id="user-1")

        self.assertEqual(result["id"], "ticket-1")
        self.assertEqual(len(result["messages"]), 2)

    def test_non_owner_is_rejected(self) -> None:
        client = _FakeClient()
        svc = _service(client)
        with self.assertRaises(SupportTicketUnauthorizedError):
            svc.get_ticket_thread(ticket_id="ticket-1", user_id="someone-else")

    def test_viewing_marks_unread_flag_false_for_that_audience(self) -> None:
        client = _FakeClient()
        svc = _service(client)

        svc.get_ticket_thread(ticket_id="ticket-1", user_id="user-1")
        patch = next(r for r in client.requests if r["method"] == "PATCH" and "support_tickets" in r["url"])
        self.assertFalse(patch["json"]["unread_by_user"])

    def test_admin_view_includes_user_email(self) -> None:
        client = _FakeClient()
        user_repo = Mock()
        user_repo._get_auth_user.return_value = {"id": "user-1", "email": "sam@example.com"}
        svc = _service(client, user_repository=user_repo)

        result = svc.get_ticket_thread(ticket_id="ticket-1", user_id=None)

        self.assertEqual(result["userEmail"], "sam@example.com")

    def test_unknown_ticket_raises_not_found(self) -> None:
        client = _FakeClient()
        svc = _service(client)
        with self.assertRaises(SupportTicketNotFoundError):
            svc.get_ticket_thread(ticket_id="does-not-exist", user_id="user-1")


class ReplyAsUserTest(unittest.TestCase):
    def test_appends_message_and_flags_unread_for_admin(self) -> None:
        client = _FakeClient()
        svc = _service(client)

        svc.reply_as_user(user_id="user-1", ticket_id="ticket-1", body="Still happening")

        patch = next(r for r in client.requests if r["method"] == "PATCH" and "support_tickets" in r["url"])
        self.assertTrue(patch["json"]["unread_by_admin"])

    def test_replying_to_a_resolved_ticket_reopens_it(self) -> None:
        client = _FakeClient(ticket_status="resolved")
        svc = _service(client)

        svc.reply_as_user(user_id="user-1", ticket_id="ticket-1", body="It's back")

        patch = next(r for r in client.requests if r["method"] == "PATCH" and "support_tickets" in r["url"])
        self.assertEqual(patch["json"]["status"], "open")
        self.assertIsNone(patch["json"]["resolved_at"])

    def test_rejects_reply_to_someone_elses_ticket(self) -> None:
        client = _FakeClient()
        svc = _service(client)
        with self.assertRaises(SupportTicketUnauthorizedError):
            svc.reply_as_user(user_id="not-the-owner", ticket_id="ticket-1", body="hi")


class ReplyAsAdminTest(unittest.TestCase):
    def test_sends_push_and_email_on_reply(self) -> None:
        client = _FakeClient()
        push = Mock()
        email = Mock(is_configured=True)
        user_repo = Mock()
        user_repo._get_auth_user.return_value = {"id": "user-1", "email": "sam@example.com"}
        svc = _service(client, push_service=push, email_service=email, user_repository=user_repo)

        svc.reply_as_admin(ticket_id="ticket-1", body="We're looking into it.", actor="support@packlox.com")

        push.dispatch_to_user.assert_called_once()
        self.assertEqual(push.dispatch_to_user.call_args.kwargs["user_id"], "user-1")
        self.assertFalse(push.dispatch_to_user.call_args.kwargs["dry_run"])
        email.send_ticket_reply_notification.assert_called_once()
        self.assertEqual(email.send_ticket_reply_notification.call_args.kwargs["to"], "sam@example.com")

    def test_default_actor_is_a_real_display_name_not_the_internal_auth_mode(
        self,
    ) -> None:
        # This endpoint authenticates with a single shared admin token (see
        # require_admin_job_token), not a per-person login, so it has no
        # real identity to attribute a reply to. Real bug found live: the
        # old default ("admin_token") -- an internal auth-mode string, not
        # a name -- was stored as sender_label and leaked straight into the
        # user-facing chat UI as if it were the replier's name.
        client = _FakeClient()
        svc = _service(client)

        svc.reply_as_admin(ticket_id="ticket-1", body="No actor supplied")

        insert = next(
            r
            for r in client.requests
            if r["method"] == "POST" and "support_messages" in r["url"]
        )
        self.assertEqual(insert["json"]["sender_label"], "PackLox Support")
        self.assertNotEqual(insert["json"]["sender_label"], "admin_token")

    def test_sets_first_response_at_only_once(self) -> None:
        client = _FakeClient(first_response_at="2026-08-01T00:00:00Z")
        svc = _service(client)

        svc.reply_as_admin(ticket_id="ticket-1", body="Second reply")

        patch = next(r for r in client.requests if r["method"] == "PATCH" and "support_tickets" in r["url"])
        self.assertNotIn("first_response_at", patch["json"])

    def test_a_failed_push_does_not_block_the_reply(self) -> None:
        client = _FakeClient()
        push = Mock()
        push.dispatch_to_user.side_effect = RuntimeError("FCM down")
        svc = _service(client, push_service=push, email_service=Mock(is_configured=False))

        result = svc.reply_as_admin(ticket_id="ticket-1", body="Reply anyway")

        self.assertEqual(result["lastMessageId"], "message-1")

    def test_email_is_skipped_when_not_configured(self) -> None:
        client = _FakeClient()
        email = Mock(is_configured=False)
        svc = _service(client, email_service=email)

        svc.reply_as_admin(ticket_id="ticket-1", body="Reply")

        email.send_ticket_reply_notification.assert_not_called()


class SetTicketStatusTest(unittest.TestCase):
    def test_resolving_sets_resolved_at(self) -> None:
        client = _FakeClient()
        svc = _service(client)

        svc.set_ticket_status(ticket_id="ticket-1", status="resolved")

        patch = next(r for r in client.requests if r["method"] == "PATCH" and "support_tickets" in r["url"])
        self.assertEqual(patch["json"]["status"], "resolved")
        self.assertIsNotNone(patch["json"]["resolved_at"])

    def test_reopening_clears_resolved_at(self) -> None:
        client = _FakeClient()
        svc = _service(client)

        svc.set_ticket_status(ticket_id="ticket-1", status="open")

        patch = next(r for r in client.requests if r["method"] == "PATCH" and "support_tickets" in r["url"])
        self.assertIsNone(patch["json"]["resolved_at"])

    def test_rejects_unknown_status(self) -> None:
        svc = _service(_FakeClient())
        with self.assertRaises(SupportTicketError):
            svc.set_ticket_status(ticket_id="ticket-1", status="archived")

    def test_resolving_notifies_the_user_by_push_and_email(self) -> None:
        # Real gap found live: an admin can resolve a ticket without
        # replying first (e.g. it was already handled elsewhere), and
        # nothing told the user at all -- no push, no email, not even an
        # unread marker in their ticket list.
        client = _FakeClient()
        push = Mock()
        email = Mock(is_configured=True)
        user_repo = Mock()
        user_repo._get_auth_user.return_value = {"id": "user-1", "email": "sam@example.com"}
        svc = _service(client, push_service=push, email_service=email, user_repository=user_repo)

        svc.set_ticket_status(ticket_id="ticket-1", status="resolved")

        push.dispatch_to_user.assert_called_once()
        self.assertEqual(push.dispatch_to_user.call_args.kwargs["user_id"], "user-1")
        self.assertEqual(
            push.dispatch_to_user.call_args.kwargs["kind"], "support_ticket_resolved",
        )
        email.send_ticket_resolved_notification.assert_called_once()
        self.assertEqual(
            email.send_ticket_resolved_notification.call_args.kwargs["to"], "sam@example.com",
        )

    def test_resolving_marks_the_ticket_unread_by_user(self) -> None:
        client = _FakeClient()
        svc = _service(client)

        svc.set_ticket_status(ticket_id="ticket-1", status="resolved")

        patch = next(r for r in client.requests if r["method"] == "PATCH" and "support_tickets" in r["url"])
        self.assertTrue(patch["json"]["unread_by_user"])

    def test_reopening_does_not_notify_or_mark_unread(self) -> None:
        # Only resolving is a user-facing event worth a notification --
        # an admin reopening a ticket is an internal housekeeping action.
        client = _FakeClient()
        push = Mock()
        email = Mock(is_configured=True)
        svc = _service(client, push_service=push, email_service=email)

        svc.set_ticket_status(ticket_id="ticket-1", status="open")

        push.dispatch_to_user.assert_not_called()
        email.send_ticket_resolved_notification.assert_not_called()
        patch = next(r for r in client.requests if r["method"] == "PATCH" and "support_tickets" in r["url"])
        self.assertNotIn("unread_by_user", patch["json"])

    def test_a_failed_resolution_notification_does_not_block_the_status_change(
        self,
    ) -> None:
        client = _FakeClient()
        push = Mock()
        push.dispatch_to_user.side_effect = RuntimeError("FCM down")
        svc = _service(client, push_service=push, email_service=Mock(is_configured=False))

        # Must not raise -- a failed push can't block the status change.
        svc.set_ticket_status(ticket_id="ticket-1", status="resolved")

        patch = next(r for r in client.requests if r["method"] == "PATCH" and "support_tickets" in r["url"])
        self.assertEqual(patch["json"]["status"], "resolved")


class AttachmentTest(unittest.TestCase):
    def test_rejects_unsupported_content_type(self) -> None:
        svc = _service(_FakeClient())
        with self.assertRaises(SupportTicketError):
            svc.add_attachment(message_id="message-1", file_name="x.exe", content_type="application/x-msdownload", raw_bytes=b"1")

    def test_rejects_oversized_file(self) -> None:
        svc = _service(_FakeClient())
        with self.assertRaises(SupportTicketError):
            svc.add_attachment(
                message_id="message-1", file_name="big.png", content_type="image/png",
                raw_bytes=b"0" * (11 * 1024 * 1024),
            )

    def test_rejects_over_the_per_message_attachment_cap(self) -> None:
        client = _FakeClient(existing_attachment_count=MAX_ATTACHMENTS_PER_MESSAGE)
        svc = _service(client)
        with self.assertRaises(SupportTicketError):
            svc.add_attachment(message_id="message-1", file_name="x.png", content_type="image/png", raw_bytes=b"1")

    def test_rejects_upload_to_a_message_on_someone_elses_ticket(self) -> None:
        client = _FakeClient()
        svc = _service(client)
        with self.assertRaises(SupportTicketUnauthorizedError):
            svc.add_attachment(
                message_id="message-1", file_name="x.png", content_type="image/png",
                raw_bytes=b"1", user_id="not-the-owner",
            )

    def test_successful_upload_returns_a_signed_url(self) -> None:
        client = _FakeClient()
        svc = _service(client)

        result = svc.add_attachment(
            message_id="message-1", file_name="screenshot.png", content_type="image/png",
            raw_bytes=b"pngdata", user_id="user-1",
        )

        self.assertEqual(result["fileName"], "screenshot.png")
        self.assertIsNotNone(result["url"])
        upload = next(r for r in client.requests if r["method"] == "POST" and "/storage/v1/object/" in r["url"] and "/sign/" not in r["url"])
        self.assertIn("support-attachments/message-1/screenshot.png", upload["url"])


class AdminListTicketsTest(unittest.TestCase):
    def test_includes_kpis(self) -> None:
        client = _FakeClient()
        user_repo = Mock()
        user_repo._get_auth_user.return_value = {"email": "user@example.com"}
        svc = _service(client, user_repository=user_repo)

        payload = svc.list_tickets()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["openCount"], 3)
        self.assertIsInstance(payload["tickets"], list)


class _FakeClient:
    def __init__(
        self,
        *,
        ticket_status: str = "open",
        first_response_at: str | None = None,
        existing_attachment_count: int = 0,
    ) -> None:
        self.requests: list[dict] = []
        self._ticket_status = ticket_status
        self._first_response_at = first_response_at
        self._existing_attachment_count = existing_attachment_count

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        params = kwargs.get("params") or {}

        if url.endswith("/auth/v1/user"):
            return _response({"id": "user-1"})

        if "/storage/v1/object/sign/" in url:
            return _response({"signedURL": "/object/sign/bucket/path?token=abc"})

        if "/storage/v1/object/" in url and method == "POST":
            return _response({"Key": "uploaded"})

        if "/rest/v1/support_tickets" in url:
            if method == "POST":
                return _response([{
                    "id": "ticket-1", "user_id": "user-1", "category": kwargs["json"]["category"],
                    "subject": kwargs["json"]["subject"], "status": "open",
                    "unread_by_admin": True, "unread_by_user": False,
                }])
            if method == "PATCH":
                return _response(None)
            if method == "GET":
                if kwargs.get("headers", {}).get("Prefer") == "count=exact":
                    return _response([], headers={"content-range": "0-0/3"})
                if params.get("id") == "eq.ticket-1":
                    return _response([{
                        "id": "ticket-1", "user_id": "user-1", "category": "bug", "subject": "App crashed",
                        "status": self._ticket_status, "unread_by_admin": True, "unread_by_user": True,
                        "first_response_at": self._first_response_at, "resolved_at": None,
                    }])
                if params.get("id") == "eq.does-not-exist":
                    return _response([])
                return _response([{
                    "id": "ticket-1", "user_id": "user-1", "category": "bug", "subject": "App crashed",
                    "status": "open", "unread_by_admin": False, "unread_by_user": False,
                }])

        if "/rest/v1/support_messages" in url:
            if method == "POST":
                return _response([{"id": "message-1", "ticket_id": kwargs["json"]["ticket_id"]}])
            if method == "GET":
                return _response([
                    {"id": "message-1", "ticket_id": "ticket-1", "sender_type": "user", "body": "It crashed", "created_at": "2026-08-01T00:00:00Z"},
                    {"id": "message-2", "ticket_id": "ticket-1", "sender_type": "admin", "sender_label": "support@packlox.com", "body": "Looking into it", "created_at": "2026-08-01T01:00:00Z"},
                ])

        if "/rest/v1/support_message_attachments" in url:
            if method == "GET":
                return _response([{"id": f"att-{i}"} for i in range(self._existing_attachment_count)])
            if method == "POST":
                return _response([{
                    "id": "att-new", "message_id": kwargs["json"]["message_id"],
                    "file_name": kwargs["json"]["file_name"], "content_type": kwargs["json"]["content_type"],
                    "size_bytes": kwargs["json"]["size_bytes"],
                }])

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
