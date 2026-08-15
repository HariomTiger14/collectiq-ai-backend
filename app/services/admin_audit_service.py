from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import settings


class AdminAuditError(Exception):
    """Raised when admin audit events cannot be read or written."""


class AdminAuditService:
    def __init__(
        self,
        *,
        repository: "SupabaseAdminAuditRepository | None" = None,
    ) -> None:
        self._repository = repository or SupabaseAdminAuditRepository()

    def record(
        self,
        *,
        action: str,
        status: str,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str = "admin_token",
    ) -> dict[str, Any]:
        event = {
            "id": str(uuid4()),
            "createdAt": _utc_now(),
            "actor": actor,
            "action": action,
            "status": status,
            "targetType": target_type,
            "targetId": target_id,
            "metadata": metadata or {},
        }
        if self._repository.is_configured:
            return self._repository.record(event)
        _IN_MEMORY_AUDIT_EVENTS.insert(0, event)
        return event

    def list_events(
        self,
        *,
        action: str | None = None,
        status: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        oldest_first: bool = False,
    ) -> dict[str, Any]:
        if self._repository.is_configured:
            events = self._repository.list_events(
                action=action,
                status=status,
                target_type=target_type,
                target_id=target_id,
                actor=actor,
                since=since,
                until=until,
                limit=limit,
                oldest_first=oldest_first,
            )
        else:
            matching = [
                event
                for event in _IN_MEMORY_AUDIT_EVENTS
                if _event_matches(
                    event,
                    action=action,
                    status=status,
                    target_type=target_type,
                    target_id=target_id,
                    actor=actor,
                    since=since,
                    until=until,
                )
            ]
            # _IN_MEMORY_AUDIT_EVENTS is newest-first (record() inserts at
            # index 0) -- reverse before truncating so oldest_first actually
            # returns the oldest matches, not the newest N reversed.
            events = (list(reversed(matching)) if oldest_first else matching)[:limit]
        return {
            "success": True,
            "count": len(events),
            "events": events,
        }

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        if self._repository.is_configured:
            return self._repository.get_event(event_id)
        return next(
            (event for event in _IN_MEMORY_AUDIT_EVENTS if str(event.get("id")) == event_id),
            None,
        )


class SupabaseAdminAuditRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        table_name: str = "admin_audit_events",
        timeout_seconds: float = 5,
        client: httpx.Client | None = None,
    ) -> None:
        self._supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).strip().rstrip("/")
        self._service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        ).strip()
        self._table_name = table_name.strip() or "admin_audit_events"
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def record(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"/rest/v1/{self._table_name}",
            json_payload=_row_from_event(event),
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(payload, list) and payload:
            row = payload[0]
            if isinstance(row, dict):
                return _event_from_row(row)
        return event

    def list_events(
        self,
        *,
        action: str | None = None,
        status: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        oldest_first: bool = False,
    ) -> list[dict[str, Any]]:
        params = {
            "select": "*",
            "limit": str(limit),
            "order": "created_at.asc" if oldest_first else "created_at.desc",
        }
        if action:
            params["action"] = f"eq.{action}"
        if status:
            params["status"] = f"eq.{status}"
        if target_type:
            params["target_type"] = f"eq.{target_type}"
        if target_id:
            params["target_id"] = f"eq.{target_id}"
        if actor:
            params["actor"] = f"eq.{actor}"
        if since and until:
            params["and"] = f"(created_at.gte.{since},created_at.lte.{until})"
        elif since:
            params["created_at"] = f"gte.{since}"
        elif until:
            params["created_at"] = f"lte.{until}"

        payload = self._request(
            "GET",
            f"/rest/v1/{self._table_name}",
            params=params,
        )
        if not isinstance(payload, list):
            raise AdminAuditError("Supabase audit response shape was invalid.")
        return [_event_from_row(row) for row in payload if isinstance(row, dict)]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        payload = self._request(
            "GET",
            f"/rest/v1/{self._table_name}",
            params={"id": f"eq.{event_id}", "select": "*", "limit": "1"},
        )
        if not isinstance(payload, list):
            raise AdminAuditError("Supabase audit response shape was invalid.")
        if not payload:
            return None
        row = payload[0]
        return _event_from_row(row) if isinstance(row, dict) else None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.request(
                method,
                f"{self._supabase_url}{path}",
                headers=headers,
                params=params,
                json=json_payload,
            )
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AdminAuditError("Supabase admin audit request failed.") from error
        finally:
            if should_close:
                client.close()


def _event_matches(
    event: dict[str, Any],
    *,
    action: str | None,
    status: str | None,
    target_type: str | None,
    target_id: str | None,
    actor: str | None,
    since: str | None,
    until: str | None,
) -> bool:
    if action and event.get("action") != action:
        return False
    if status and event.get("status") != status:
        return False
    if target_type and event.get("targetType") != target_type:
        return False
    if target_id and event.get("targetId") != target_id:
        return False
    if actor and event.get("actor") != actor:
        return False
    created_at = str(event.get("createdAt") or "")
    if since and created_at < since:
        return False
    if until and created_at > until:
        return False
    return True


def clear_in_memory_audit_events() -> None:
    _IN_MEMORY_AUDIT_EVENTS.clear()


def _row_from_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event["id"],
        "created_at": event["createdAt"],
        "actor": event["actor"],
        "action": event["action"],
        "status": event["status"],
        "target_type": event.get("targetType"),
        "target_id": event.get("targetId"),
        "metadata": event.get("metadata") or {},
    }


def _event_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "createdAt": str(row.get("created_at") or row.get("createdAt") or ""),
        "actor": str(row.get("actor") or "admin_token"),
        "action": str(row.get("action") or ""),
        "status": str(row.get("status") or "unknown"),
        "targetType": row.get("target_type") or row.get("targetType"),
        "targetId": row.get("target_id") or row.get("targetId"),
        "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_IN_MEMORY_AUDIT_EVENTS: list[dict[str, Any]] = []
