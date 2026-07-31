from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import settings


class AdminImportJobError(Exception):
    """Raised when admin import job persistence cannot be completed."""


class AdminImportJobService:
    def __init__(
        self,
        *,
        repository: "SupabaseAdminImportJobRepository | None" = None,
    ) -> None:
        self._repository = repository or SupabaseAdminImportJobRepository()

    def create_job(self, *, source: str, dry_run: bool, actor: str = "admin_token") -> dict[str, Any]:
        job = {
            "id": str(uuid4()),
            "createdAt": _utc_now(),
            "updatedAt": _utc_now(),
            "completedAt": None,
            "actor": actor,
            "source": source or "all",
            "dryRun": dry_run,
            "status": "running",
            "inputRows": 0,
            "validRows": 0,
            "importedRows": 0,
            "historyRows": 0,
            "error": None,
        }
        return self._save(job)

    def complete_job(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        update = {
            "status": "complete",
            "completedAt": _utc_now(),
            "updatedAt": _utc_now(),
            "inputRows": int(payload.get("inputRows") or 0),
            "validRows": int(payload.get("validRows") or 0),
            "importedRows": int(payload.get("importedRows") or 0),
            "historyRows": int(payload.get("historyRows") or 0),
            "error": None,
        }
        return self._update(job_id, update)

    def fail_job(self, job_id: str, message: str) -> dict[str, Any]:
        return self._update(
            job_id,
            {
                "status": "failed",
                "completedAt": _utc_now(),
                "updatedAt": _utc_now(),
                "error": message,
            },
        )

    def list_jobs(self, *, limit: int = 25) -> dict[str, Any]:
        if self._repository.is_configured:
            try:
                jobs = self._repository.list_jobs(limit=limit)
            except AdminImportJobError:
                jobs = _IN_MEMORY_IMPORT_JOBS[:limit]
        else:
            jobs = _IN_MEMORY_IMPORT_JOBS[:limit]
        return {"success": True, "count": len(jobs), "jobs": jobs}

    def _save(self, job: dict[str, Any]) -> dict[str, Any]:
        if self._repository.is_configured:
            try:
                return self._repository.create_job(job)
            except AdminImportJobError:
                pass
        _IN_MEMORY_IMPORT_JOBS.insert(0, job)
        return job

    def _update(self, job_id: str, update: dict[str, Any]) -> dict[str, Any]:
        if self._repository.is_configured:
            try:
                return self._repository.update_job(job_id, update)
            except AdminImportJobError:
                pass
        for job in _IN_MEMORY_IMPORT_JOBS:
            if job.get("id") == job_id:
                job.update(update)
                return job
        raise AdminImportJobError("Import job was not found.")


class SupabaseAdminImportJobRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        table_name: str = "admin_import_jobs",
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
        self._table_name = table_name.strip() or "admin_import_jobs"
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def create_job(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"/rest/v1/{self._table_name}",
            json_payload=_row_from_job(job),
            extra_headers={"Prefer": "return=representation"},
        )
        return _job_from_response(payload, job)

    def update_job(self, job_id: str, update: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "PATCH",
            f"/rest/v1/{self._table_name}",
            params={"id": f"eq.{job_id}", "select": "*"},
            json_payload=_row_from_job(update),
            extra_headers={"Prefer": "return=representation"},
        )
        return _job_from_response(payload, {"id": job_id, **update})

    def list_jobs(self, *, limit: int) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/rest/v1/{self._table_name}",
            params={"select": "*", "order": "created_at.desc", "limit": str(limit)},
        )
        if not isinstance(payload, list):
            raise AdminImportJobError("Import job response shape was invalid.")
        return [_job_from_row(row) for row in payload if isinstance(row, dict)]

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
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
            raise AdminImportJobError("Supabase import job request failed.") from error
        finally:
            if should_close:
                client.close()


def _row_from_job(job: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "id": "id",
        "createdAt": "created_at",
        "updatedAt": "updated_at",
        "completedAt": "completed_at",
        "actor": "actor",
        "source": "source",
        "dryRun": "dry_run",
        "status": "status",
        "inputRows": "input_rows",
        "validRows": "valid_rows",
        "importedRows": "imported_rows",
        "historyRows": "history_rows",
        "error": "error",
    }
    return {target: job[source] for source, target in mapping.items() if source in job}


def _job_from_response(payload: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return _job_from_row(payload[0])
    return fallback


def _job_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "completedAt": row.get("completed_at"),
        "actor": row.get("actor") or "admin_token",
        "source": row.get("source") or "all",
        "dryRun": bool(row.get("dry_run")),
        "status": row.get("status") or "unknown",
        "inputRows": int(row.get("input_rows") or 0),
        "validRows": int(row.get("valid_rows") or 0),
        "importedRows": int(row.get("imported_rows") or 0),
        "historyRows": int(row.get("history_rows") or 0),
        "error": row.get("error"),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_IN_MEMORY_IMPORT_JOBS: list[dict[str, Any]] = []
