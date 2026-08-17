from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.pricing.admin_review_queue_service import _total_from_content_range
from app.services.pricing.catalog_search_service import (
    _POKEMON_SET_TCGPLAYER_GROUPS,
    _funko_lookup_title,
    _lego_name_words,
    _lego_set_number,
    _lego_title_before_number,
    _magic_card_name,
    _magic_card_number,
    _magic_set_name_from_console,
    _normalize_magic_text,
    _lorcana_set_name_from_console,
    _onepiece_meaningful_words,
    _onepiece_set_code,
    _yugioh_set_code,
    _normalize_variant_words,
    _pokemon_card_number,
    _pokemon_variant_token,
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
            items = self._enrich_pokemon_images(items)
            items = self._enrich_lego_images(items)
            items = self._enrich_magic_images(items)
            items = self._enrich_yugioh_images(items)
            items = self._enrich_lorcana_images(items)
            items = self._enrich_onepiece_images(items)
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

    def _enrich_pokemon_images(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Mirrors CatalogSearchService._enrich_with_pokemon_image -- same
        # small, verified _POKEMON_SET_TCGPLAYER_GROUPS mapping and the
        # same print-variant safety rules (exact match first, suppress
        # rather than guess when sibling variant rows exist, plain match
        # otherwise), so the admin browse table shows the same images
        # -- and the same gaps -- the mobile/public search does. See that
        # method's comment for the full reasoning. tcgplayer_pokemon_
        # catalog is our own small Supabase table (imported from TCGCSV),
        # so this is one cheap indexed query per matched row, not a live
        # third-party call.
        targets = [
            (index, item)
            for index, item in enumerate(items)
            if not item.get("imageUrl")
            and item.get("setName")
            and str(item["setName"]).strip().lower() in _POKEMON_SET_TCGPLAYER_GROUPS
        ]
        for index, item in targets:
            group_name = _POKEMON_SET_TCGPLAYER_GROUPS[str(item["setName"]).strip().lower()]
            card_number = _pokemon_card_number(str(item.get("title") or ""))
            if not card_number:
                continue
            variant_token = _pokemon_variant_token(str(item.get("title") or ""))
            image_url = self._repository.fetch_tcgplayer_exact_variant_image(
                group_name, card_number, variant_token
            )
            if image_url:
                items[index]["imageUrl"] = image_url
                continue
            if self._repository.has_sibling_pokemon_rows(
                str(item["setName"]), card_number, exclude_id=str(item.get("id") or "")
            ):
                continue
            generic_image_url = self._repository.fetch_tcgplayer_generic_image(
                group_name, card_number
            )
            if generic_image_url:
                items[index]["imageUrl"] = generic_image_url
        return items

    def _enrich_lego_images(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Mirrors CatalogSearchService._enrich_with_lego_image -- same
        # Rebrickable-backed base-number-plus-word-overlap safety check,
        # so the admin browse table shows the same images -- and avoids
        # the same false-number-match risk -- the mobile/public search
        # does. One batched lookup per page covers every distinct LEGO
        # set number on it, not one request per row (same pattern as the
        # Funko enrichment above).
        targets = [
            (index, item)
            for index, item in enumerate(items)
            if not item.get("imageUrl")
            and item.get("setName")
            and "lego" in str(item["setName"]).lower()
        ]
        lookup_by_index: dict[int, tuple[str, set[str]]] = {}
        for index, item in targets:
            title = str(item.get("title") or "")
            base_number = _lego_set_number(title)
            if base_number is None:
                continue
            title_words = _lego_name_words(_lego_title_before_number(title))
            if not title_words:
                continue
            lookup_by_index[index] = (base_number, title_words)
        base_numbers = sorted({base_number for base_number, _ in lookup_by_index.values()})
        if not base_numbers:
            return items
        candidates_by_number = self._repository.fetch_lego_candidates(base_numbers)
        for index, (base_number, title_words) in lookup_by_index.items():
            for candidate in candidates_by_number.get(base_number, []):
                candidate_words = _lego_name_words(str(candidate.get("name") or ""))
                if title_words & candidate_words:
                    image_url = candidate.get("image_url")
                    if image_url:
                        items[index]["imageUrl"] = str(image_url)
                    break
        return items

    def _enrich_magic_images(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Mirrors CatalogSearchService._enrich_with_magic_image -- same
        # Scryfall-backed number-first / name-fallback matching, so the
        # admin browse table shows the same images the mobile/public
        # search does. Batched once per distinct Magic set on the page
        # (collector numbers and names are set-scoped, so the lookup
        # naturally groups by set), not one request per row.
        targets = [
            (index, item)
            for index, item in enumerate(items)
            if not item.get("imageUrl")
            and item.get("setName")
            and "magic" in str(item["setName"]).lower()
        ]
        by_set: dict[str, list[tuple[int, str | None, str]]] = {}
        for index, item in targets:
            set_name = _magic_set_name_from_console(str(item["setName"]))
            if set_name is None:
                continue
            title = str(item.get("title") or "")
            card_number = _magic_card_number(title)
            normalized_name = "" if card_number is not None else _normalize_magic_text(_magic_card_name(title))
            if card_number is None and not normalized_name:
                continue
            by_set.setdefault(_normalize_magic_text(set_name), []).append(
                (index, card_number, normalized_name)
            )
        for normalized_set_name, entries in by_set.items():
            numbers = sorted({number for _, number, _ in entries if number is not None})
            names = sorted({name for _, number, name in entries if number is None and name})
            by_number, by_name = self._repository.fetch_magic_candidates(
                normalized_set_name, numbers=numbers, names=names
            )
            for index, card_number, normalized_name in entries:
                image_url = by_number.get(card_number) if card_number is not None else by_name.get(normalized_name)
                if image_url:
                    items[index]["imageUrl"] = image_url
        return items

    def _enrich_yugioh_images(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Mirrors CatalogSearchService._enrich_with_yugioh_image -- same
        # global set_code lookup (no per-set resolution needed), so the
        # admin browse table shows the same images the mobile/public
        # search does. One batched lookup per page covers every distinct
        # set code on it, not one request per row (same pattern as the
        # Funko/LEGO enrichment above).
        lookup_by_index: dict[int, str] = {}
        for index, item in enumerate(items):
            if item.get("imageUrl") or not item.get("setName"):
                continue
            if "yugioh" not in str(item["setName"]).lower():
                continue
            set_code = _yugioh_set_code(str(item.get("title") or ""))
            if set_code:
                lookup_by_index[index] = set_code
        set_codes = sorted(set(lookup_by_index.values()))
        if not set_codes:
            return items
        images_by_code = self._repository.fetch_yugioh_images(set_codes)
        for index, set_code in lookup_by_index.items():
            image_url = images_by_code.get(set_code)
            if image_url:
                items[index]["imageUrl"] = image_url
        return items

    def _enrich_lorcana_images(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Mirrors CatalogSearchService._enrich_with_lorcana_image -- same
        # normalized-set-name + card-number lookup, so the admin browse
        # table shows the same images the mobile/public search does.
        # Batched once per distinct Lorcana set on the page (card numbers
        # are set-scoped, so the lookup naturally groups by set), not one
        # request per row.
        by_set: dict[str, list[tuple[int, str]]] = {}
        for index, item in enumerate(items):
            if item.get("imageUrl") or not item.get("setName"):
                continue
            if "lorcana" not in str(item["setName"]).lower():
                continue
            set_name = _lorcana_set_name_from_console(str(item["setName"]))
            if set_name is None:
                continue
            card_number = _lego_set_number(str(item.get("title") or ""))
            if card_number is None:
                continue
            by_set.setdefault(_normalize_magic_text(set_name), []).append((index, card_number))
        for normalized_set_name, entries in by_set.items():
            numbers = sorted({number for _, number in entries})
            images_by_number = self._repository.fetch_lorcana_images(normalized_set_name, numbers)
            for index, card_number in entries:
                image_url = images_by_number.get(card_number)
                if image_url:
                    items[index]["imageUrl"] = image_url
        return items

    def _enrich_onepiece_images(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Mirrors CatalogSearchService._enrich_with_onepiece_image -- same
        # is_plain / word-overlap disambiguation, so the admin browse
        # table shows the same images -- and the same conservative gaps
        # -- the mobile/public search does. Batched once per distinct set
        # code on the page, not one request per row.
        lookup_by_index: dict[int, tuple[str, str | None]] = {}
        for index, item in enumerate(items):
            if item.get("imageUrl") or not item.get("setName"):
                continue
            if "one piece" not in str(item["setName"]).lower():
                continue
            title = str(item.get("title") or "")
            set_code = _onepiece_set_code(title)
            if set_code is None:
                continue
            lookup_by_index[index] = (set_code, _pokemon_variant_token(title))
        set_codes = sorted({code for code, _ in lookup_by_index.values()})
        if not set_codes:
            return items
        rows_by_code = self._repository.fetch_onepiece_rows(set_codes)
        for index, (set_code, variant_token) in lookup_by_index.items():
            rows = rows_by_code.get(set_code, [])
            if not rows:
                continue
            if variant_token is None:
                plain_rows = [row for row in rows if row.get("is_plain")]
                if len(plain_rows) == 1:
                    image_url = plain_rows[0].get("image_url")
                    if image_url:
                        items[index]["imageUrl"] = str(image_url)
                continue
            variant_words = _onepiece_meaningful_words(variant_token)
            if not variant_words:
                continue
            # Strict subset, and only when it uniquely identifies one
            # candidate -- see CatalogSearchService._enrich_with_
            # onepiece_image for why "any word in common" produced real
            # wrong matches.
            matches = [
                row
                for row in rows
                if not row.get("is_plain")
                and variant_words.issubset(_onepiece_meaningful_words(str(row.get("card_name") or "")))
            ]
            if len(matches) == 1:
                image_url = matches[0].get("image_url")
                if image_url:
                    items[index]["imageUrl"] = str(image_url)
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

    def fetch_lego_candidates(self, base_numbers: list[str]) -> dict[str, list[dict[str, Any]]]:
        # One batched request for every distinct LEGO set number on a
        # page -- see AdminCatalogService._enrich_lego_images. The word-
        # overlap safety check itself happens in the caller, against
        # whichever candidates share a base_number.
        unique_numbers = sorted({number for number in base_numbers if number})
        if not unique_numbers:
            return {}
        quoted = ",".join(f'"{number}"' for number in unique_numbers)
        payload = self._request(
            "GET",
            "/rest/v1/rebrickable_lego_catalog",
            params={
                "select": "base_number,name,image_url",
                "base_number": f"in.({quoted})",
            },
        )
        if not isinstance(payload, list):
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            grouped.setdefault(str(row.get("base_number") or ""), []).append(row)
        return grouped

    def fetch_magic_candidates(
        self, normalized_set_name: str, *, numbers: list[str], names: list[str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        # One batched request per lookup type (number-based, name-based)
        # for a whole Magic set on the page -- see AdminCatalogService.
        # _enrich_magic_images. Collector numbers are guaranteed unique
        # within a Scryfall set, but names are not (a reprinted basic
        # land with no distinguishing number can appear more than once)
        # -- either way, more than one row for the same key means no
        # confident single image, so it's dropped rather than guessed.
        by_number = self._fetch_magic_unique_matches(
            normalized_set_name, column="collector_number", values=numbers
        )
        by_name = self._fetch_magic_unique_matches(
            normalized_set_name, column="normalized_name", values=names
        )
        return by_number, by_name

    def _fetch_magic_unique_matches(
        self, normalized_set_name: str, *, column: str, values: list[str]
    ) -> dict[str, str]:
        unique_values = sorted({value for value in values if value})
        if not unique_values:
            return {}
        quoted = ",".join(f'"{value}"' for value in unique_values)
        payload = self._request(
            "GET",
            "/rest/v1/scryfall_magic_catalog",
            params={
                "select": f"{column},image_url",
                "normalized_set_name": f"eq.{normalized_set_name}",
                column: f"in.({quoted})",
            },
        )
        if not isinstance(payload, list):
            return {}
        counts: dict[str, int] = {}
        images: dict[str, str] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            key = str(row.get(column) or "")
            counts[key] = counts.get(key, 0) + 1
            image_url = row.get("image_url")
            if image_url:
                images[key] = str(image_url)
        return {key: url for key, url in images.items() if counts.get(key) == 1}

    def fetch_yugioh_images(self, set_codes: list[str]) -> dict[str, str]:
        # One batched request for every distinct Yu-Gi-Oh set code on a
        # page -- see AdminCatalogService._enrich_yugioh_images. set_code
        # is the table's primary key, so no ambiguity check is needed
        # here the way Magic/LEGO need one -- import time already
        # resolved that (see yugioh_catalog's migration comment).
        unique_codes = sorted({code for code in set_codes if code})
        if not unique_codes:
            return {}
        quoted = ",".join(f'"{code}"' for code in unique_codes)
        payload = self._request(
            "GET",
            "/rest/v1/yugioh_catalog",
            params={
                "select": "set_code,image_url",
                "set_code": f"in.({quoted})",
            },
        )
        if not isinstance(payload, list):
            return {}
        images_by_code: dict[str, str] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            code = str(row.get("set_code") or "")
            image_url = row.get("image_url")
            if code and image_url:
                images_by_code[code] = str(image_url)
        return images_by_code

    def fetch_lorcana_images(self, normalized_set_name: str, numbers: list[str]) -> dict[str, str]:
        # One batched request per distinct Lorcana set on the page -- see
        # AdminCatalogService._enrich_lorcana_images. (normalized_set_name,
        # card_number) is the table's primary key, so no ambiguity check
        # is needed here the way Magic/LEGO need one.
        unique_numbers = sorted({number for number in numbers if number})
        if not unique_numbers:
            return {}
        quoted = ",".join(f'"{number}"' for number in unique_numbers)
        payload = self._request(
            "GET",
            "/rest/v1/lorcana_catalog",
            params={
                "select": "card_number,image_url",
                "normalized_set_name": f"eq.{normalized_set_name}",
                "card_number": f"in.({quoted})",
            },
        )
        if not isinstance(payload, list):
            return {}
        images_by_number: dict[str, str] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            number = str(row.get("card_number") or "")
            image_url = row.get("image_url")
            if number and image_url:
                images_by_number[number] = str(image_url)
        return images_by_number

    def fetch_onepiece_rows(self, set_codes: list[str]) -> dict[str, list[dict[str, Any]]]:
        # One batched request for every distinct One Piece set code on a
        # page -- see AdminCatalogService._enrich_onepiece_images. The
        # is_plain / word-overlap disambiguation itself happens in the
        # caller, against whichever rows share a card_set_id.
        unique_codes = sorted({code for code in set_codes if code})
        if not unique_codes:
            return {}
        quoted = ",".join(f'"{code}"' for code in unique_codes)
        payload = self._request(
            "GET",
            "/rest/v1/one_piece_catalog",
            params={
                "select": "card_set_id,card_name,is_plain,image_url",
                "card_set_id": f"in.({quoted})",
            },
        )
        if not isinstance(payload, list):
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            grouped.setdefault(str(row.get("card_set_id") or ""), []).append(row)
        return grouped

    def _fetch_tcgplayer_rows(self, group_name: str, card_number: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/rest/v1/tcgplayer_pokemon_catalog",
            params={
                "select": "product_name,image_url,variant_tag",
                "group_name": f"eq.{group_name}",
                "card_number": f"eq.{card_number}",
            },
        )
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def fetch_tcgplayer_exact_variant_image(
        self, group_name: str, card_number: str, variant_token: str | None
    ) -> str | None:
        # Mirrors CatalogSearchService._fetch_tcgplayer_exact_variant_image
        # -- see that method for why Shadowless (a separate TCGCSV group)
        # and named error products (variant_tag='error', matched by full
        # word-overlap with the PriceCharting bracket tag) are the only
        # two patterns treated as an exact print-variant match.
        if not variant_token:
            return None
        if "shadowless" in variant_token:
            for row in self._fetch_tcgplayer_rows(f"{group_name} (Shadowless)", card_number):
                image_url = row.get("image_url")
                if image_url:
                    return str(image_url)
            return None
        variant_words = _normalize_variant_words(variant_token)
        if not variant_words:
            return None
        for row in self._fetch_tcgplayer_rows(group_name, card_number):
            if row.get("variant_tag") != "error":
                continue
            product_words = _normalize_variant_words(str(row.get("product_name") or ""))
            if variant_words.issubset(product_words) and row.get("image_url"):
                return str(row["image_url"])
        return None

    def fetch_tcgplayer_generic_image(self, group_name: str, card_number: str) -> str | None:
        rows = [
            row
            for row in self._fetch_tcgplayer_rows(group_name, card_number)
            if not row.get("variant_tag")
        ]
        if len(rows) != 1:
            return None
        image_url = rows[0].get("image_url")
        return str(image_url) if image_url else None

    def has_sibling_pokemon_rows(
        self, set_name: str, card_number: str, *, exclude_id: str
    ) -> bool:
        payload = self._request(
            "GET",
            "/rest/v1/pricecharting_catalog",
            params={
                "select": "pricecharting_id",
                "console_name": f"eq.{set_name}",
                "product_name": f"ilike.*#{card_number}",
                "limit": "3",
            },
        )
        if not isinstance(payload, list):
            return False
        return any(
            str(row.get("pricecharting_id")) != str(exclude_id)
            for row in payload
            if isinstance(row, dict)
        )

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
