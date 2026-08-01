from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import settings


class AdminNotesError(Exception):
    """Raised when admin notes cannot be read or written."""


class AdminNotesService:
    def __init__(
        self,
        *,
        repository: "SupabaseAdminNotesRepository | None" = None,
    ) -> None:
        self._repository = repository or SupabaseAdminNotesRepository()

    def list_notes(self, *, target_type: str, target_id: str, limit: int = 50) -> dict[str, Any]:
        if self._repository.is_configured:
            notes = self._repository.list_notes(
                target_type=target_type,
                target_id=target_id,
                limit=limit,
            )
        else:
            notes = [
                note
                for note in _IN_MEMORY_NOTES
                if note["targetType"] == target_type and note["targetId"] == target_id
            ][:limit]
        return {"success": True, "count": len(notes), "notes": notes}

    def create_note(
        self,
        *,
        target_type: str,
        target_id: str,
        note: str,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "id": str(uuid4()),
            "targetType": target_type,
            "targetId": target_id,
            "note": note.strip(),
            "actor": actor,
            "metadata": metadata or {},
            "createdAt": _utc_now(),
            "updatedAt": _utc_now(),
        }
        if not payload["note"]:
            raise AdminNotesError("Note text is required.")
        if self._repository.is_configured:
            created = self._repository.create_note(payload)
        else:
            _IN_MEMORY_NOTES.insert(0, payload)
            created = payload
        return {"success": True, "note": created}


class SupabaseAdminNotesRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        table_name: str = "admin_notes",
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
        self._table_name = table_name.strip() or "admin_notes"
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def list_notes(self, *, target_type: str, target_id: str, limit: int) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/rest/v1/{self._table_name}",
            params={
                "select": "*",
                "target_type": f"eq.{target_type}",
                "target_id": f"eq.{target_id}",
                "order": "created_at.desc",
                "limit": str(limit),
            },
        )
        if not isinstance(payload, list):
            raise AdminNotesError("Supabase admin notes response shape was invalid.")
        return [_note_from_row(row) for row in payload if isinstance(row, dict)]

    def create_note(self, note: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"/rest/v1/{self._table_name}",
            json_payload=_row_from_note(note),
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return _note_from_row(payload[0])
        return note

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
            raise AdminNotesError("Supabase admin notes request failed.") from error
        finally:
            if should_close:
                client.close()


def clear_in_memory_notes() -> None:
    _IN_MEMORY_NOTES.clear()


def _note_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "targetType": str(row.get("target_type") or ""),
        "targetId": str(row.get("target_id") or ""),
        "note": str(row.get("note") or ""),
        "actor": str(row.get("actor") or ""),
        "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


def _row_from_note(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": note["id"],
        "target_type": note["targetType"],
        "target_id": note["targetId"],
        "note": note["note"],
        "actor": note["actor"],
        "metadata": note.get("metadata") or {},
        "created_at": note["createdAt"],
        "updated_at": note["updatedAt"],
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_IN_MEMORY_NOTES: list[dict[str, Any]] = []
