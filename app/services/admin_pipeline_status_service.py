"""Admin visibility into the PriceCharting registry backfill and KicksDB
catalog refresh -- the numbers that were previously only checkable via
hand-run SQL queries against Supabase during live tuning of those crons.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings


class AdminPipelineStatusError(Exception):
    """Raised when pipeline status data cannot be read safely."""


class AdminPipelineStatusService:
    def __init__(
        self,
        *,
        repository: "SupabasePipelineStatusRepository | None" = None,
    ) -> None:
        self._repository = repository or SupabasePipelineStatusRepository()

    def get_summary(self) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminPipelineStatusError("Supabase admin configuration is missing.")
        registry_rows = self._repository.registry_summary()
        kicksdb = self._repository.kicksdb_summary()
        categories = [_compact_registry_row(row) for row in registry_rows]
        totals = _totals_from_categories(categories)
        return {
            "success": True,
            "priceCharting": {
                "categories": categories,
                "totals": totals,
            },
            "kicksDb": _compact_kicksdb_summary(kicksdb),
        }


class SupabasePipelineStatusRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        timeout_seconds: float = 15,
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
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def registry_summary(self) -> list[dict[str, Any]]:
        payload = self._rpc("admin_pricecharting_registry_summary")
        if not isinstance(payload, list):
            raise AdminPipelineStatusError(
                "Supabase registry summary response shape was invalid."
            )
        return [row for row in payload if isinstance(row, dict)]

    def kicksdb_summary(self) -> dict[str, Any]:
        payload = self._rpc("admin_kicksdb_catalog_summary")
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        raise AdminPipelineStatusError(
            "Supabase KicksDB summary response shape was invalid."
        )

    def _rpc(self, function_name: str) -> Any:
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.post(
                f"{self._supabase_url}/rest/v1/rpc/{function_name}",
                headers=headers,
                json={},
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AdminPipelineStatusError(
                f"Supabase RPC {function_name} request failed."
            ) from error
        finally:
            if should_close:
                client.close()


def _compact_registry_row(row: dict[str, Any]) -> dict[str, Any]:
    total = int(row.get("total_sets") or 0)
    completed = int(row.get("completed_sets") or 0)
    return {
        "sourceSite": row.get("source_site"),
        "category": row.get("category"),
        "totalSets": total,
        "completedSets": completed,
        "failedSets": int(row.get("failed_sets") or 0),
        "pendingSets": int(row.get("pending_sets") or 0),
        "priorityTier": row.get("priority_tier"),
        "percentComplete": round((completed / total) * 100, 1) if total else 0.0,
    }


def _totals_from_categories(categories: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(c["totalSets"] for c in categories)
    completed = sum(c["completedSets"] for c in categories)
    return {
        "totalSets": total,
        "completedSets": completed,
        "percentComplete": round((completed / total) * 100, 1) if total else 0.0,
    }


def _compact_kicksdb_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "totalItems": int(row.get("total_items") or 0),
        "distinctBrands": int(row.get("distinct_brands") or 0),
        "lastRefreshedAt": row.get("last_refreshed_at"),
    }
