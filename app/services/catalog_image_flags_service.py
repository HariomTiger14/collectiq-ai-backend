from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings

KNOWN_CATEGORIES: tuple[str, ...] = (
    "funko",
    "pokemon",
    "lego",
    "magic",
    "yugioh",
    "lorcana",
    "onepiece",
)


class CatalogImageFlagsError(Exception):
    """Raised when catalog image source flags cannot be read or written."""


class UnknownCatalogImageCategoryError(CatalogImageFlagsError):
    """Raised when toggling a category that isn't one of the known image sources."""


class CatalogImageFlagsService:
    def __init__(
        self,
        *,
        repository: "SupabaseCatalogImageFlagsRepository | None" = None,
    ) -> None:
        self._repository = repository or SupabaseCatalogImageFlagsRepository()

    def list_flags(self) -> dict[str, Any]:
        if self._repository.is_configured:
            rows_by_category = {
                row["category"]: row for row in self._repository.list_flags()
            }
        else:
            rows_by_category = {}
        flags = [
            rows_by_category.get(category)
            or {"category": category, "enabled": True, "updatedAt": None}
            for category in KNOWN_CATEGORIES
        ]
        return {"success": True, "flags": flags}

    def set_flag(self, *, category: str, enabled: bool) -> dict[str, Any]:
        normalized_category = category.strip().lower()
        if normalized_category not in KNOWN_CATEGORIES:
            raise UnknownCatalogImageCategoryError(
                f"'{category}' is not a known catalog image category."
            )
        if self._repository.is_configured:
            updated = self._repository.set_flag(
                category=normalized_category, enabled=enabled
            )
        else:
            updated = {
                "category": normalized_category,
                "enabled": enabled,
                "updatedAt": _utc_now(),
            }
        return {"success": True, "flag": updated}


class SupabaseCatalogImageFlagsRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        table_name: str = "catalog_image_source_flags",
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
        self._table_name = table_name.strip() or "catalog_image_source_flags"
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def list_flags(self) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/rest/v1/{self._table_name}",
            params={"select": "category,enabled,updated_at", "order": "category.asc"},
        )
        if not isinstance(payload, list):
            raise CatalogImageFlagsError(
                "Supabase catalog image flags response shape was invalid."
            )
        return [_flag_from_row(row) for row in payload if isinstance(row, dict)]

    def set_flag(self, *, category: str, enabled: bool) -> dict[str, Any]:
        payload = self._request(
            "PATCH",
            f"/rest/v1/{self._table_name}",
            params={"category": f"eq.{category}"},
            json_payload={"enabled": enabled, "updated_at": _utc_now()},
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return _flag_from_row(payload[0])
        raise UnknownCatalogImageCategoryError(
            f"'{category}' is not a known catalog image category."
        )

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
            raise CatalogImageFlagsError(
                "Supabase catalog image flags request failed."
            ) from error
        finally:
            if should_close:
                client.close()


def _flag_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": str(row.get("category") or ""),
        "enabled": bool(row.get("enabled", True)),
        "updatedAt": row.get("updated_at"),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
