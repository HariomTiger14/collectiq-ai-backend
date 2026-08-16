from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.pricing.admin_review_queue_service import _total_from_content_range
from app.services.pricing.catalog_search_service import (
    _funko_lookup_title,
    select_best_funko_image,
)


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
        self,
        *,
        source: str = "pricecharting",
        limit: int = 100,
        offset: int = 0,
        category: str | None = None,
        category_group: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminCatalogError("Supabase catalog configuration is missing.")
        normalized_source = source if source in ("pricecharting", "kicksdb") else "pricecharting"
        bounded_limit = max(1, min(limit, 100))
        rows = self._repository.list_catalog_rows(
            source=normalized_source, limit=bounded_limit, offset=max(0, offset),
            category=category, category_group=category_group, min_price=min_price, max_price=max_price,
        )
        items = [_compact_catalog_row(row, source=normalized_source) for row in rows]
        if normalized_source == "pricecharting":
            items = self._enrich_funko_images(items)
        return {
            "success": True,
            "source": normalized_source,
            "count": len(rows),
            "totalCount": self._repository.count_catalog_rows(
                source=normalized_source, category=category, category_group=category_group,
                min_price=min_price, max_price=max_price,
            ),
            "items": items,
        }

    def _enrich_funko_images(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # PriceCharting has no image data at all (confirmed live — see
        # catalog_search_service.py). One batched lookup per page covers
        # every Funko row on it, not one request per row.
        lookup_by_index = {
            index: _funko_lookup_title(str(item.get("title") or ""))
            for index, item in enumerate(items)
            if item.get("setName") and "funko" in str(item["setName"]).lower()
        }
        titles = sorted({title for title in lookup_by_index.values() if title})
        if not titles:
            return items
        images_by_title = self._repository.fetch_funko_images(titles)
        if not images_by_title:
            return items
        for index, lookup_title in lookup_by_index.items():
            image_url = images_by_title.get(lookup_title)
            if image_url:
                items[index]["imageUrl"] = image_url
        return items


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
        self,
        *,
        source: str,
        limit: int,
        offset: int,
        category: str | None = None,
        category_group: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
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
        params = {"select": "*", "order": order, "limit": str(limit), "offset": str(offset)}
        params.update(_catalog_filter_params(
            source, category=category, category_group=category_group, min_price=min_price, max_price=max_price,
        ))
        payload = self._request("GET", f"/rest/v1/{table_name}", params=params)
        if not isinstance(payload, list):
            raise AdminCatalogError("Supabase catalog response shape was invalid.")
        return [row for row in payload if isinstance(row, dict)]

    def count_catalog_rows(
        self,
        *,
        source: str,
        category: str | None = None,
        category_group: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> int:
        # count=estimated, not count=exact: pricecharting_catalog has ~43k
        # rows and RLS enabled (20260811_enable_rls_on_catalog_and_admin_
        # tables.sql), so an exact COUNT(*) forces a full RLS-filtered scan.
        # This table already has a documented production incident from an
        # unrelated expensive-query mistake (statement timeouts, see
        # 20260808_drop_unused_pricecharting_catalog_indexes.sql) -- not
        # worth risking a repeat for a pagination total that doesn't need
        # to be perfectly exact. PostgREST's estimated mode uses the
        # planner's row estimate for large tables, falling back to an exact
        # count when the result set is already small -- this holds even
        # with the same filters applied, since the planner's estimate
        # already accounts for filter selectivity.
        table_name = "kicksdb_catalog" if source == "kicksdb" else "pricecharting_catalog"
        id_column = "kicksdb_id" if source == "kicksdb" else "pricecharting_id"
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Prefer": "count=estimated",
        }
        params = {"select": id_column, "limit": "1"}
        params.update(_catalog_filter_params(
            source, category=category, category_group=category_group, min_price=min_price, max_price=max_price,
        ))
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.request(
                "GET",
                f"{self._supabase_url}/rest/v1/{table_name}",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            return _total_from_content_range(response.headers.get("content-range"))
        except httpx.HTTPError as error:
            raise AdminCatalogError("Supabase catalog count request failed.") from error
        finally:
            if should_close:
                client.close()

    def fetch_funko_images(self, normalized_titles: list[str]) -> dict[str, str]:
        # One batched request for every distinct Funko title on a page,
        # not one request per row -- see AdminCatalogService._enrich_funko_
        # images. PostgREST's in.() filter needs each value double-quoted
        # so titles containing spaces are treated as single list entries.
        unique_titles = sorted({title for title in normalized_titles if title})
        if not unique_titles:
            return {}
        quoted = ",".join(f'"{title}"' for title in unique_titles)
        payload = self._request(
            "GET",
            "/rest/v1/funko_pop_catalog",
            params={
                "select": "normalized_title,image_url,series",
                "normalized_title": f"in.({quoted})",
            },
        )
        if not isinstance(payload, list):
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            grouped.setdefault(str(row.get("normalized_title") or ""), []).append(row)
        images_by_title: dict[str, str] = {}
        for title, candidates in grouped.items():
            image_url = select_best_funko_image(candidates)
            if image_url:
                images_by_title[title] = image_url
        return images_by_title

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


# pricecharting_catalog's raw `category` column is far too granular for a
# dropdown ("Basketball Cards 2019 Panini Donruss Optic", not "Sports
# Cards") -- there's no separate coarse-category column, so these groups
# are keyword sets or'd together against the same raw column. Directly
# grounded in the taxonomy this codebase already tracks elsewhere (the
# Catalog page's own "PriceCharting set backfill" panel groups sets into
# exactly coins/comic-books/funko-pops/lego-sets/lorcana-cards/*-cards),
# plus trading-card-games for Magic/Pokemon/Yugioh, which are clearly
# present in the raw data but aren't one of that panel's pipeline buckets.
# KicksDB has no equivalent taxonomy defined anywhere in this system, so
# it isn't included here -- its category filter stays free text.
PRICECHARTING_CATEGORY_GROUPS: dict[str, list[str]] = {
    "sports-cards": ["Baseball", "Basketball", "Football", "Hockey", "Soccer"],
    "trading-card-games": ["Magic", "Pokemon", "Yugioh", "Lorcana"],
    "comics": ["Comic"],
    "funko-pops": ["Funko"],
    "lego-sets": ["Lego"],
    "coins": ["Coin"],
}


def _catalog_filter_params(
    source: str,
    *,
    category: str | None,
    category_group: str | None = None,
    min_price: float | None,
    max_price: float | None,
) -> dict[str, str]:
    params: dict[str, str] = {}
    keywords = PRICECHARTING_CATEGORY_GROUPS.get(category_group or "") if source != "kicksdb" else None
    if keywords:
        params["or"] = "(" + ",".join(f"category.ilike.*{kw}*" for kw in keywords) + ")"
    elif category:
        params["category"] = f"ilike.*{category}*"
    if min_price is None and max_price is None:
        return params
    # A single representative price column per source, not every tier a
    # pricecharting_catalog row can carry (loose/cib/new/graded) -- there's
    # no single "market value" column to filter on, and PostgREST can't
    # express "whichever of these four is populated" as a plain filter.
    # loose is the most commonly populated tier in practice; an item priced
    # only on a different tier won't match a range filter here. Same
    # approximation _compact_catalog_row already makes for display.
    price_column = "avg_price_cents" if source == "kicksdb" else "loose_price_cents"
    min_cents = int(min_price * 100) if min_price is not None else None
    max_cents = int(max_price * 100) if max_price is not None else None
    if min_cents is not None and max_cents is not None:
        params["and"] = f"({price_column}.gte.{min_cents},{price_column}.lte.{max_cents})"
    elif min_cents is not None:
        params[price_column] = f"gte.{min_cents}"
    else:
        params[price_column] = f"lte.{max_cents}"
    return params


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
            "imageUrl": row.get("image_url"),
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
        "imageUrl": None,
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
