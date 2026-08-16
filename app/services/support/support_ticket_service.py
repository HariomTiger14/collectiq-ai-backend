"""Real support ticketing: a threaded conversation between a user and an
admin, replacing the mobile app's plain mailto "Contact support" link and
the admin console's static Support inbox mockup.

Two-way notification on an admin reply: a push (reusing the same FCM
pipeline built for price alerts / broadcast in `PriceAlertPushService`) and
an email via Resend. Both are best-effort -- a failed notification never
blocks the reply itself from being saved, matching the audit-log write
pattern used elsewhere in this codebase (a missed side-effect shouldn't
break the primary action).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.admin_user_service import SupabaseAdminUserRepository
from app.services.email.resend_email_service import ResendEmailService
from app.services.push.price_alert_push_service import PriceAlertPushService

ATTACHMENT_BUCKET = "collectiq-portfolio-images"
ATTACHMENT_PREFIX = "support-attachments"
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 5
ALLOWED_ATTACHMENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/heic",
    "image/webp",
    "text/csv",
    "application/vnd.ms-excel",
}
CATEGORIES = ("bug", "pricing", "question", "feedback")


class SupportTicketError(Exception):
    """Raised when a support ticket action cannot complete safely."""


class SupportTicketUnauthorizedError(SupportTicketError):
    """The caller's Supabase session is missing, invalid, or not the owner."""


class SupportTicketNotFoundError(SupportTicketError):
    pass


class SupportTicketService:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        anon_key: str | None = None,
        client: httpx.Client | None = None,
        push_service: PriceAlertPushService | None = None,
        email_service: ResendEmailService | None = None,
        user_repository: SupabaseAdminUserRepository | None = None,
    ) -> None:
        self._supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).strip().rstrip("/")
        self._service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        ).strip()
        self._anon_key = (
            anon_key if anon_key is not None else settings.supabase_anon_key
        ).strip()
        self._client = client or httpx.Client(timeout=20)
        self._push_service = push_service or PriceAlertPushService()
        self._email_service = email_service or ResendEmailService()
        self._user_repository = user_repository or SupabaseAdminUserRepository()

    # -- auth (user-facing endpoints only) -------------------------------

    def user_id_from_token(self, access_token: str) -> str:
        token = (access_token or "").strip()
        if not token:
            raise SupportTicketUnauthorizedError("Missing access token.")
        try:
            response = self._client.get(
                f"{self._supabase_url}/auth/v1/user",
                headers={"apikey": self._anon_key, "Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as error:
            raise SupportTicketError(f"Could not reach the auth service: {error}") from error
        if response.status_code != 200:
            raise SupportTicketUnauthorizedError("Invalid or expired session.")
        user_id = (response.json() or {}).get("id")
        if not user_id:
            raise SupportTicketUnauthorizedError("Could not resolve user.")
        return str(user_id)

    # -- user-facing -------------------------------------------------------

    def create_ticket(
        self,
        *,
        user_id: str,
        category: str,
        subject: str,
        message: str,
        referenced_item_id: str | None = None,
    ) -> dict[str, Any]:
        category = (category or "").strip().lower()
        subject = (subject or "").strip()
        message = (message or "").strip()
        if category not in CATEGORIES:
            raise SupportTicketError(f"Unknown category: {category!r}")
        if not subject or not message:
            raise SupportTicketError("Subject and message are both required.")

        rows = self._request(
            "POST",
            "/rest/v1/support_tickets",
            json_payload={
                "user_id": user_id,
                "category": category,
                "subject": subject,
                "referenced_item_id": referenced_item_id,
            },
            extra_headers={"Prefer": "return=representation"},
        )
        if not isinstance(rows, list) or not rows:
            raise SupportTicketError("Could not create the ticket.")
        ticket = rows[0]

        message_id = self._insert_message(
            ticket["id"], sender_type="user", sender_label=None, body=message,
        )
        result = _ticket_public(ticket)
        result["lastMessageId"] = message_id
        return result

    def list_my_tickets(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._request(
            "GET",
            "/rest/v1/support_tickets",
            params={
                "user_id": f"eq.{user_id}",
                "select": "*",
                "order": "updated_at.desc",
            },
        )
        return [_ticket_public(row) for row in rows] if isinstance(rows, list) else []

    def get_ticket_thread(
        self, *, ticket_id: str, user_id: str | None = None,
    ) -> dict[str, Any]:
        """user_id=None means an admin caller (no ownership check)."""
        ticket = self._get_ticket_row(ticket_id)
        if user_id is not None and str(ticket.get("user_id")) != str(user_id):
            raise SupportTicketUnauthorizedError("This ticket does not belong to you.")
        messages = self._fetch_messages(ticket_id)
        if user_id is not None and ticket.get("unread_by_user"):
            self._patch_ticket(ticket_id, {"unread_by_user": False})
        elif user_id is None and ticket.get("unread_by_admin"):
            self._patch_ticket(ticket_id, {"unread_by_admin": False})
        result = _ticket_public(ticket)
        result["messages"] = messages
        if user_id is None:
            auth_user = self._user_repository._get_auth_user(str(ticket.get("user_id") or "")) or {}
            result["userEmail"] = auth_user.get("email")
        return result

    def reply_as_user(self, *, user_id: str, ticket_id: str, body: str) -> dict[str, Any]:
        body = (body or "").strip()
        if not body:
            raise SupportTicketError("A message is required.")
        ticket = self._get_ticket_row(ticket_id)
        if str(ticket.get("user_id")) != str(user_id):
            raise SupportTicketUnauthorizedError("This ticket does not belong to you.")
        message_id = self._insert_message(ticket_id, sender_type="user", sender_label=None, body=body)
        patch: dict[str, Any] = {"unread_by_admin": True}
        if ticket.get("status") == "resolved":
            patch["status"] = "open"
            patch["resolved_at"] = None
        self._patch_ticket(ticket_id, patch)
        result = self.get_ticket_thread(ticket_id=ticket_id, user_id=user_id)
        result["lastMessageId"] = message_id
        return result

    def add_attachment(
        self,
        *,
        message_id: str,
        file_name: str,
        content_type: str,
        raw_bytes: bytes,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """user_id=None means an admin caller (no ownership check)."""
        if user_id is not None:
            rows = self._request(
                "GET",
                "/rest/v1/support_messages",
                params={"id": f"eq.{message_id}", "select": "ticket_id", "limit": "1"},
            )
            if not isinstance(rows, list) or not rows:
                raise SupportTicketNotFoundError(f"Message {message_id} was not found.")
            ticket = self._get_ticket_row(str(rows[0]["ticket_id"]))
            if str(ticket.get("user_id")) != str(user_id):
                raise SupportTicketUnauthorizedError("This message does not belong to you.")
        if content_type not in ALLOWED_ATTACHMENT_TYPES:
            raise SupportTicketError(f"Unsupported file type: {content_type!r}")
        if len(raw_bytes) > MAX_ATTACHMENT_BYTES:
            raise SupportTicketError("File is larger than the 10 MB limit.")
        existing = self._request(
            "GET",
            "/rest/v1/support_message_attachments",
            params={"message_id": f"eq.{message_id}", "select": "id"},
        )
        if isinstance(existing, list) and len(existing) >= MAX_ATTACHMENTS_PER_MESSAGE:
            raise SupportTicketError(
                f"A message can have at most {MAX_ATTACHMENTS_PER_MESSAGE} attachments."
            )

        file_path = f"{ATTACHMENT_PREFIX}/{message_id}/{file_name}"
        self._request(
            "POST",
            f"/storage/v1/object/{ATTACHMENT_BUCKET}/{file_path}",
            raw_body=raw_bytes,
            extra_headers={"Content-Type": content_type, "x-upsert": "true"},
        )
        rows = self._request(
            "POST",
            "/rest/v1/support_message_attachments",
            json_payload={
                "message_id": message_id,
                "file_path": file_path,
                "file_name": file_name,
                "content_type": content_type,
                "size_bytes": len(raw_bytes),
            },
            extra_headers={"Prefer": "return=representation"},
        )
        if not isinstance(rows, list) or not rows:
            raise SupportTicketError("Could not save the attachment.")
        return _attachment_public(rows[0], signed_url=self._signed_attachment_url(file_path))

    # -- admin -----------------------------------------------------------

    def list_tickets(self, *, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        params = {"select": "*", "order": "updated_at.desc", "limit": str(limit)}
        if status:
            params["status"] = f"eq.{status}"
        rows = self._request("GET", "/rest/v1/support_tickets", params=params)
        tickets = [_ticket_public(row) for row in rows] if isinstance(rows, list) else []
        user_ids = list({t["userId"] for t in tickets if t.get("userId")})
        emails = self._batch_emails(user_ids)
        for ticket in tickets:
            ticket["userLabel"] = emails.get(ticket.get("userId"), ticket.get("userId"))
        return {
            "success": True,
            "tickets": tickets,
            "openCount": self._count(status="open"),
            "closed30d": self._count(
                status="resolved",
                since=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
            ),
            "avgFirstResponseHours": self._avg_first_response_hours(),
        }

    def reply_as_admin(
        self, *, ticket_id: str, body: str, actor: str = "admin_token",
    ) -> dict[str, Any]:
        body = (body or "").strip()
        if not body:
            raise SupportTicketError("A message is required.")
        ticket = self._get_ticket_row(ticket_id)
        message_id = self._insert_message(ticket_id, sender_type="admin", sender_label=actor, body=body)

        patch: dict[str, Any] = {"unread_by_user": True}
        if not ticket.get("first_response_at"):
            patch["first_response_at"] = datetime.now(timezone.utc).isoformat()
        self._patch_ticket(ticket_id, patch)

        self._notify_user_of_reply(ticket=ticket, reply_body=body)
        result = self.get_ticket_thread(ticket_id=ticket_id, user_id=None)
        result["lastMessageId"] = message_id
        return result

    def set_ticket_status(
        self, *, ticket_id: str, status: str, actor: str = "admin_token",
    ) -> dict[str, Any]:
        if status not in ("open", "resolved"):
            raise SupportTicketError(f"Unknown status: {status!r}")
        self._get_ticket_row(ticket_id)
        patch: dict[str, Any] = {"status": status}
        patch["resolved_at"] = datetime.now(timezone.utc).isoformat() if status == "resolved" else None
        self._patch_ticket(ticket_id, patch)
        return self.get_ticket_thread(ticket_id=ticket_id, user_id=None)

    # -- notification (best-effort, never blocks the reply) ----------------

    def _notify_user_of_reply(self, *, ticket: dict[str, Any], reply_body: str) -> None:
        user_id = str(ticket.get("user_id") or "")
        if not user_id:
            return
        try:
            self._push_service.dispatch_to_user(
                user_id=user_id,
                title="New reply on your support ticket",
                body=reply_body[:180],
                dry_run=False,
                kind="support_ticket_reply",
                notification_data={
                    "type": "support_ticket_reply",
                    "ticketId": str(ticket.get("id") or ""),
                },
            )
        except Exception:  # noqa: BLE001 - a missed push can't block the reply
            pass

        try:
            auth_user = self._user_repository._get_auth_user(user_id) or {}
            email = auth_user.get("email")
            if email and self._email_service.is_configured:
                self._email_service.send_ticket_reply_notification(
                    to=email,
                    subject=str(ticket.get("subject") or "your support ticket"),
                    reply_body=reply_body,
                )
        except Exception:  # noqa: BLE001 - a missed email can't block the reply
            pass

    # -- shared helpers ----------------------------------------------------

    def _get_ticket_row(self, ticket_id: str) -> dict[str, Any]:
        rows = self._request(
            "GET",
            "/rest/v1/support_tickets",
            params={"id": f"eq.{ticket_id}", "select": "*", "limit": "1"},
        )
        if not isinstance(rows, list) or not rows:
            raise SupportTicketNotFoundError(f"Support ticket {ticket_id} was not found.")
        return rows[0]

    def _patch_ticket(self, ticket_id: str, patch: dict[str, Any]) -> None:
        self._request(
            "PATCH",
            "/rest/v1/support_tickets",
            params={"id": f"eq.{ticket_id}"},
            json_payload={**patch, "updated_at": datetime.now(timezone.utc).isoformat()},
            extra_headers={"Prefer": "return=minimal"},
        )

    def _insert_message(
        self, ticket_id: str, *, sender_type: str, sender_label: str | None, body: str,
    ) -> str | None:
        rows = self._request(
            "POST",
            "/rest/v1/support_messages",
            json_payload={
                "ticket_id": ticket_id,
                "sender_type": sender_type,
                "sender_label": sender_label,
                "body": body,
            },
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0].get("id")
        return None

    def _fetch_messages(self, ticket_id: str) -> list[dict[str, Any]]:
        rows = self._request(
            "GET",
            "/rest/v1/support_messages",
            params={
                "ticket_id": f"eq.{ticket_id}",
                "select": "*",
                "order": "created_at.asc",
            },
        )
        messages = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        message_ids = [str(m["id"]) for m in messages if m.get("id")]
        attachments_by_message = self._fetch_attachments(message_ids)
        return [
            {
                "id": m.get("id"),
                "senderType": m.get("sender_type"),
                "senderLabel": m.get("sender_label"),
                "body": m.get("body"),
                "createdAt": m.get("created_at"),
                "attachments": attachments_by_message.get(str(m.get("id")), []),
            }
            for m in messages
        ]

    def _fetch_attachments(self, message_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not message_ids:
            return {}
        rows = self._request(
            "GET",
            "/rest/v1/support_message_attachments",
            params={
                "message_id": f"in.({','.join(message_ids)})",
                "select": "*",
            },
        )
        by_message: dict[str, list[dict[str, Any]]] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            message_id = str(row.get("message_id"))
            by_message.setdefault(message_id, []).append(
                _attachment_public(row, signed_url=self._signed_attachment_url(row.get("file_path")))
            )
        return by_message

    def _signed_attachment_url(self, file_path: str | None, *, expires_in: int = 3600) -> str | None:
        if not file_path:
            return None
        try:
            payload = self._request(
                "POST",
                f"/storage/v1/object/sign/{ATTACHMENT_BUCKET}/{file_path}",
                json_payload={"expiresIn": expires_in},
            )
        except SupportTicketError:
            return None
        if not isinstance(payload, dict):
            return None
        signed_path = payload.get("signedURL") or payload.get("signedUrl")
        if not isinstance(signed_path, str) or not signed_path:
            return None
        if signed_path.startswith("http"):
            return signed_path
        if not signed_path.startswith("/"):
            signed_path = "/" + signed_path
        if signed_path.startswith("/storage"):
            return f"{self._supabase_url}{signed_path}"
        return f"{self._supabase_url}/storage/v1{signed_path}"

    def _batch_emails(self, user_ids: list[str]) -> dict[str, str]:
        emails: dict[str, str] = {}
        for user_id in user_ids:
            auth_user = self._user_repository._get_auth_user(user_id) or {}
            if auth_user.get("email"):
                emails[user_id] = auth_user["email"]
        return emails

    def _count(self, *, status: str | None = None, since: str | None = None) -> int:
        params: dict[str, str] = {"select": "id"}
        if status:
            params["status"] = f"eq.{status}"
        if since:
            params["resolved_at" if status == "resolved" else "updated_at"] = f"gte.{since}"
        try:
            response = self._request(
                "GET",
                "/rest/v1/support_tickets",
                params=params,
                extra_headers={"Prefer": "count=exact", "Range": "0-0"},
                return_response=True,
            )
        except SupportTicketError:
            return 0
        return _total_from_content_range(response.headers.get("content-range"))

    def _avg_first_response_hours(self) -> float | None:
        rows = self._request(
            "GET",
            "/rest/v1/support_tickets",
            params={
                "first_response_at": "not.is.null",
                "select": "created_at,first_response_at",
                "order": "created_at.desc",
                "limit": "200",
            },
        )
        if not isinstance(rows, list) or not rows:
            return None
        deltas = []
        for row in rows:
            try:
                created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                responded = datetime.fromisoformat(str(row["first_response_at"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            deltas.append((responded - created).total_seconds() / 3600)
        if not deltas:
            return None
        return round(sum(deltas) / len(deltas), 1)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_payload: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        return_response: bool = False,
    ):
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
        }
        if json_payload is not None or raw_body is None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = self._client.request(
                method,
                f"{self._supabase_url}{path}",
                headers=headers,
                params=params,
                json=json_payload if raw_body is None else None,
                content=raw_body,
            )
            response.raise_for_status()
            if return_response:
                return response
            if not response.content:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise SupportTicketError(f"Supabase support-ticket call failed: {error}") from error


def _total_from_content_range(content_range: str | None) -> int:
    if not content_range or "/" not in content_range:
        return 0
    total = content_range.rsplit("/", 1)[-1]
    return int(total) if total.isdigit() else 0


def _ticket_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "userId": row.get("user_id"),
        "category": row.get("category"),
        "subject": row.get("subject"),
        "status": row.get("status"),
        "referencedItemId": row.get("referenced_item_id"),
        "unreadByAdmin": row.get("unread_by_admin"),
        "unreadByUser": row.get("unread_by_user"),
        "firstResponseAt": row.get("first_response_at"),
        "resolvedAt": row.get("resolved_at"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


def _attachment_public(row: dict[str, Any], *, signed_url: str | None) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "fileName": row.get("file_name"),
        "contentType": row.get("content_type"),
        "sizeBytes": row.get("size_bytes"),
        "url": signed_url,
    }
