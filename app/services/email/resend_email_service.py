"""Direct Resend API integration for backend-triggered transactional email.

Distinct from Supabase Auth's own email flow (password reset, etc, which is
routed through whatever SMTP provider is configured in the Supabase project
dashboard) -- this is application code sending arbitrary content the backend
itself decides on, starting with "an admin replied to your support ticket".
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings

RESEND_API_URL = "https://api.resend.com/emails"


class EmailNotConfiguredError(RuntimeError):
    """Raised when RESEND_API_KEY is missing -- email is always best-effort,
    never something a caller should let block a real action."""


class ResendEmailService:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        from_address: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = (api_key if api_key is not None else settings.resend_api_key).strip()
        self._from_address = (
            from_address if from_address is not None else settings.resend_from_address
        ).strip()
        self._client = client or httpx.Client(timeout=15)

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._from_address)

    def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str | None = None,
    ) -> dict[str, Any]:
        if not self.is_configured:
            raise EmailNotConfiguredError("Resend is not configured (RESEND_API_KEY missing).")
        payload: dict[str, Any] = {
            "from": self._from_address,
            "to": [to],
            "subject": subject,
            "html": html,
        }
        if text:
            payload["text"] = text
        response = self._client.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def send_ticket_reply_notification(
        self,
        *,
        to: str,
        subject: str,
        reply_body: str,
    ) -> dict[str, Any]:
        html = (
            "<p>You have a new reply on your PackLox support ticket:</p>"
            f"<blockquote style=\"border-left:3px solid #2563EB;margin:12px 0;"
            f"padding:4px 12px;color:#333\">{_escape_html(reply_body)}</blockquote>"
            "<p>Open the PackLox app and go to Settings &rarr; Support to reply.</p>"
        )
        text = (
            f"You have a new reply on your PackLox support ticket:\n\n{reply_body}\n\n"
            "Open the PackLox app and go to Settings > Support to reply."
        )
        return self.send(
            to=to,
            subject=f"Re: {subject}",
            html=html,
            text=text,
        )


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
