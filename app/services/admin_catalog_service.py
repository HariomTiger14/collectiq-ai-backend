from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings


class AdminCatalogError(Exception):
    """Raised when admin catalog writes cannot be completed."""


class AdminCatalogService:
    def __init__(
        self,
        *,
        repository: "SupabaseAdminCatalogRepository | None" = None,
    ) -> None:
        self._repository = repository or SupabaseAdminCatalogRepository()

    def update_item(self, catalog_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminCatalogError("Supabase catalog configuration is missing.")
        item_id = str(catalog_id or "").strip()
        if not item_id:
            raise AdminCatalogError("Catalog item id is required.")
        update = _catalog_update_payload(payload)
        if not update:
            raise AdminCatalogError("At least one catalog field is required.")
        row = self._repository.update_catalog_item(item_id, update)
        return {"success": True, "itemId": item_id, "item": row}


class SupabaseAdminCatalogRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        table_name: str = "pricecharting_catalog",
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
        self._table_name = table_name.strip() or "pricecharting_catalog"
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def update_catalog_item(self, catalog_id: str, update: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "PATCH",
            f"/rest/v1/{self._table_name}",
            params={"pricecharting_id": f"eq.{catalog_id}", "select": "*"},
            json_payload={**update, "updated_at": _utc_now()},
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        raise AdminCatalogError("Catalog item was not found.")

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
            raise AdminCatalogError("Supabase catalog update request failed.") from error
        finally:
            if should_close:
                client.close()


def _catalog_update_payload(payload: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "title": "product_name",
        "category": "category",
        "console": "console_name",
        "upc": "upc",
        "productUrl": "product_url",
        "note": "admin_note",
        "active": "active",
    }
    update: dict[str, Any] = {}
    for source, target in mapping.items():
        if source not in payload:
            continue
        value = payload.get(source)
        if isinstance(value, str):
            value = value.strip()
        if value in ("", None):
            continue
        update[target] = value
    return update


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
