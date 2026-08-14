from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.pricing.admin_review_queue_service import _total_from_content_range


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

    def list_items(
        self, *, source: str = "pricecharting", limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminCatalogError("Supabase catalog configuration is missing.")
        normalized_source = source if source in ("pricecharting", "kicksdb") else "pricecharting"
        bounded_limit = max(1, min(limit, 100))
        rows = self._repository.list_catalog_rows(
            source=normalized_source, limit=bounded_limit, offset=max(0, offset),
        )
        return {
            "success": True,
            "source": normalized_source,
            "count": len(rows),
            "totalCount": self._repository.count_catalog_rows(source=normalized_source),
            "items": [_compact_catalog_row(row, source=normalized_source) for row in rows],
        }


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

    def list_catalog_rows(
        self, *, source: str, limit: int, offset: int,
    ) -> list[dict[str, Any]]:
        # Independent of self._table_name (which stays scoped to
        # pricecharting_catalog for writes) -- this is read-only browsing
        # across whichever of the two catalog tables the caller asked for.
        #
        # Order columns are deliberately narrow: pricecharting_catalog had
        # five indexes dropped in 20260808_drop_unused_pricecharting_catalog_
        # indexes.sql after an unrelated unindexed sort (product_name.asc)
        # caused production write timeouts, and that migration's own history
        # says a naive column choice here already broke things once. Primary
        # keys are always index-backed, so pricecharting_id.asc is safe.
        # kicksdb_catalog's rank column has its own dedicated partial index
        # (kicksdb_catalog_rank_idx) and doubles as a meaningful "most
        # popular first" ordering, not just a safe one.
        table_name, order = (
            ("kicksdb_catalog", "rank.asc.nullslast")
            if source == "kicksdb"
            else ("pricecharting_catalog", "pricecharting_id.asc")
        )
        payload = self._request(
            "GET",
            f"/rest/v1/{table_name}",
            params={"select": "*", "order": order, "limit": str(limit), "offset": str(offset)},
        )
        if not isinstance(payload, list):
            raise AdminCatalogError("Supabase catalog response shape was invalid.")
        return [row for row in payload if isinstance(row, dict)]

    def count_catalog_rows(self, *, source: str) -> int:
        table_name = "kicksdb_catalog" if source == "kicksdb" else "pricecharting_catalog"
        id_column = "kicksdb_id" if source == "kicksdb" else "pricecharting_id"
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Prefer": "count=exact",
        }
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.request(
                "GET",
                f"{self._supabase_url}/rest/v1/{table_name}",
                headers=headers,
                params={"select": id_column, "limit": "1"},
            )
            response.raise_for_status()
            return _total_from_content_range(response.headers.get("content-range"))
        except httpx.HTTPError as error:
            raise AdminCatalogError("Supabase catalog count request failed.") from error
        finally:
            if should_close:
                client.close()

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
            raise AdminCatalogError("Supabase catalog request failed.") from error
        finally:
            if should_close:
                client.close()


def _compact_catalog_row(row: dict[str, Any], *, source: str) -> dict[str, Any]:
    # Deliberately matches the shape CatalogSearchResult already returns
    # (id/title/category/setName/source/lastUpdated/pricing.marketValue) so
    # the admin frontend's existing search-row renderer and edit drawer work
    # for browsed rows unchanged.
    if source == "kicksdb":
        market_value = (
            _cents_to_units(row.get("avg_price_cents"))
            or _cents_to_units(row.get("min_price_cents"))
            or _cents_to_units(row.get("max_price_cents"))
        )
        return {
            "id": row.get("kicksdb_id"),
            "title": row.get("title") or "Catalog item",
            "identifier": row.get("sku"),
            "category": row.get("category") or row.get("product_type") or "Sneaker",
            "setName": row.get("brand"),
            "source": "KicksDB",
            "lastUpdated": row.get("updated_at"),
            "pricing": {"marketValue": market_value, "currency": (row.get("currency") or "USD").upper()},
        }
    loose = _cents_to_units(row.get("loose_price_cents"))
    cib = _cents_to_units(row.get("cib_price_cents"))
    new = _cents_to_units(row.get("new_price_cents"))
    graded = _cents_to_units(row.get("graded_price_cents"))
    market_value = loose or cib or new or graded or _cents_to_units(row.get("market_value_cents"))
    return {
        "id": row.get("pricecharting_id"),
        "title": row.get("product_name") or "Catalog item",
        "identifier": row.get("upc"),
        "category": row.get("category") or row.get("console_name") or "Catalog",
        "setName": row.get("console_name"),
        "source": "PriceCharting",
        "lastUpdated": row.get("updated_at"),
        "pricing": {"marketValue": market_value, "currency": (row.get("currency") or "USD").upper()},
    }


def _cents_to_units(value: Any) -> float | None:
    if value is None:
        return None
    try:
        cents = float(value)
    except (TypeError, ValueError):
        return None
    return round(cents / 100, 2) if cents > 0 else None


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
