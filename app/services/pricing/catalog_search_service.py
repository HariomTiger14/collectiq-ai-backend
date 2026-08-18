from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.search import (
    CatalogDetailResponse,
    CatalogHistoryPoint,
    CatalogSearchPricing,
    CatalogSearchResponse,
    CatalogSearchResult,
)


class CatalogSearchError(Exception):
    """Raised when catalog search cannot be completed."""


class CatalogItemNotFoundError(CatalogSearchError):
    """Raised when a catalog item cannot be found."""


@dataclass(frozen=True)
class CatalogSearchService:
    supabase_url: str | None = None
    service_role_key: str | None = None
    timeout_seconds: float = 5
    client: httpx.Client | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def search(self, query: str, limit: int = 20) -> CatalogSearchResponse:
        normalized_query = _normalize_query(query)
        bounded_limit = max(1, min(limit, 50))
        if len(normalized_query) < 2:
            return CatalogSearchResponse(query=normalized_query, count=0, results=[])
        if not self.is_configured:
            raise CatalogSearchError("Catalog search is not configured.")

        # Two sources, merged and re-ranked together: pricecharting_catalog
        # (cards/games/comics/coins/etc.) and kicksdb_catalog (sneakers/
        # streetwear — the only source with real product images today).
        # Each source's own RPC does its own indexed filter+sort; the
        # combined list below just needs a single comparable score per row
        # to interleave the two sources correctly, so results aren't
        # PriceCharting-first / KicksDB-second regardless of relevance.
        pc_rows = self._fetch_rows(normalized_query, bounded_limit)
        kicksdb_rows = self._fetch_kicksdb_rows(normalized_query, bounded_limit)

        scored: list[tuple[int, str, str, dict[str, Any]]] = [
            (_match_score(row, normalized_query), str(row.get("product_name") or ""), "pricecharting", row)
            for row in pc_rows
        ] + [
            (_kicksdb_match_score(row, normalized_query), str(row.get("title") or ""), "kicksdb", row)
            for row in kicksdb_rows
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        top = scored[:bounded_limit]

        results = [
            _row_to_result(row, normalized_query)
            if source == "pricecharting"
            else _kicksdb_row_to_result(row, normalized_query)
            for _, _, source, row in top
        ]
        # Deliberately no inline imageUrl enrichment here: this is the open,
        # free-to-everyone catalog browse/search surface. Publisher-sourced
        # card/product art (Pokemon/Magic/Yu-Gi-Oh/Lorcana/One Piece/LEGO/
        # Funko) is only rendered inline in detail(), reached by tapping
        # into a specific item to confirm and save it to a portfolio -- a
        # bounded, per-item identification use, not an open image database.
        # We DO still resolve the same match here and expose it as
        # externalImageUrl (see _resolve_external_image_url) -- link-only,
        # opened in an external/in-app browser tab by the client, never
        # rendered inline, which is the lower-risk hyperlink pattern rather
        # than hosting the image inside our own app layout.
        # KicksDB (sneakers) images are unaffected; they come from
        # _kicksdb_row_to_result above, not this enrichment chain.
        if any(not result.imageUrl for result in results):
            enabled_image_categories = self._fetch_enabled_image_categories()
            results = [
                self._resolve_external_image_url(result, enabled_image_categories)
                for result in results
            ]
        return CatalogSearchResponse(
            query=normalized_query,
            count=len(results),
            results=results,
        )

    def detail(self, catalog_id: str, history_limit: int = 30) -> CatalogDetailResponse:
        normalized_id = str(catalog_id or "").strip()
        bounded_history_limit = max(1, min(history_limit, 90))
        if not normalized_id:
            raise CatalogItemNotFoundError("Catalog item was not found.")
        if not self.is_configured:
            raise CatalogSearchError("Catalog search is not configured.")

        row = self._fetch_catalog_row(normalized_id)
        if row is not None:
            history_rows = self._fetch_history_rows(normalized_id, bounded_history_limit)
            result = _row_to_result(
                row,
                _normalize_query(str(row.get("product_name") or "")),
            )
            enabled = self._fetch_enabled_image_categories()
            if "funko" in enabled:
                result = self._enrich_with_funko_image(result)
            if "pokemon" in enabled:
                result = self._enrich_with_pokemon_image(result)
            if "lego" in enabled:
                result = self._enrich_with_lego_image(result)
            if "magic" in enabled:
                result = self._enrich_with_magic_image(result)
            if "yugioh" in enabled:
                result = self._enrich_with_yugioh_image(result)
            if "lorcana" in enabled:
                result = self._enrich_with_lorcana_image(result)
            if "onepiece" in enabled:
                result = self._enrich_with_onepiece_image(result)
            if "videogames" in enabled:
                # Positioned last, deliberately: this is the only
                # enrichment in the chain that makes a live network call
                # to a third party (RAWG) rather than only ever hitting
                # our own DB -- see _enrich_with_video_games_image. It
                # only ever runs here in detail(), never from search()'s
                # _resolve_external_image_url path, so a live RAWG call
                # is bounded to one specific item view, not fired on
                # every keystroke of an open catalog search across all
                # users (RAWG's free tier is capped at 20,000 requests/
                # month).
                result = self._enrich_with_video_games_image(result)
            return CatalogDetailResponse(
                result=result,
                history=[_history_row_to_point(row) for row in history_rows],
            )

        # Not a pricecharting_catalog row — try kicksdb_catalog before
        # giving up. The two tables have independent id spaces (no
        # collision risk), so this is a safe fallback, not a guess.
        kicksdb_row = self._fetch_kicksdb_catalog_row(normalized_id)
        if kicksdb_row is not None:
            kicksdb_history_rows = self._fetch_kicksdb_history_rows(
                normalized_id, bounded_history_limit
            )
            return CatalogDetailResponse(
                result=_kicksdb_row_to_result(
                    kicksdb_row,
                    _normalize_query(str(kicksdb_row.get("title") or "")),
                ),
                history=[
                    _kicksdb_history_row_to_point(row) for row in kicksdb_history_rows
                ],
            )

        raise CatalogItemNotFoundError("Catalog item was not found.")

    @property
    def _supabase_url(self) -> str:
        value = self.supabase_url if self.supabase_url is not None else settings.supabase_url
        return value.strip().rstrip("/")

    @property
    def _service_role_key(self) -> str:
        value = (
            self.service_role_key
            if self.service_role_key is not None
            else settings.supabase_service_role_key
        )
        return value.strip()

    def _fetch_rows(self, query: str, limit: int) -> list[dict[str, Any]]:
        # Calls search_pricecharting_catalog(), a Postgres function
        # (20260806_create_search_pricecharting_catalog_rpc.sql) that does
        # the filter + relevance scoring + sort + limit entirely in SQL.
        #
        # Two single-column ORDER BY attempts on the plain REST table query
        # were tried and reverted before this: product_name.asc forced an
        # unindexed sort and broke production; pricecharting_id.asc would
        # have been fast but systematically hidden every scan-derived row
        # from popular queries (their ids sort after all-digit PriceCharting
        # ids). Neither was safe. This RPC avoids both failure modes: it
        # ranks over the true full matching set (not an arbitrary fetch-
        # window subset), the ilike filter itself is now backed by pg_trgm
        # GIN indexes on every filtered column
        # (20260806_add_trigram_indexes_for_catalog_search.sql — confirmed
        # via EXPLAIN ANALYZE: 'pikachu v' went from 15.7s to 83ms), and the
        # ranking score has nothing to do with row identity, so it can't
        # systematically favor one id format over another. Verified against
        # real SIT data before shipping, not assumed — see
        # docs/GLOBAL_CATALOG_ARCHITECTURE.md.
        #
        # Known remaining gap, not fixed by this: an extremely broad
        # single-word query matching a large fraction of the whole table
        # (e.g. "pokemon" alone, ~21% of rows) is still slow — no index
        # helps once a filter is that unselective; Postgres's planner
        # correctly prefers a sequential scan at that point. A real fix
        # needs full-text relevance ranking (the existing
        # pricecharting_catalog_search_idx GIN/tsvector index), a separate,
        # larger, unscoped change. This was very likely already slow before
        # today, unrelated to anything changed here.
        payload = self._request(
            "POST",
            "/rest/v1/rpc/search_pricecharting_catalog",
            json_payload={"search_query": query, "result_limit": limit},
        )
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _resolve_external_image_url(
        self, result: CatalogSearchResult, enabled: set[str]
    ) -> CatalogSearchResult:
        if result.imageUrl:
            # KicksDB rows already carry a real imageUrl (rendered inline
            # today, a separate/already-accepted risk profile) -- nothing
            # to add here.
            return result
        scratch = result
        if "funko" in enabled:
            scratch = self._enrich_with_funko_image(scratch)
        if "pokemon" in enabled:
            scratch = self._enrich_with_pokemon_image(scratch)
        if "lego" in enabled:
            scratch = self._enrich_with_lego_image(scratch)
        if "magic" in enabled:
            scratch = self._enrich_with_magic_image(scratch)
        if "yugioh" in enabled:
            scratch = self._enrich_with_yugioh_image(scratch)
        if "lorcana" in enabled:
            scratch = self._enrich_with_lorcana_image(scratch)
        if "onepiece" in enabled:
            scratch = self._enrich_with_onepiece_image(scratch)
        if not scratch.imageUrl:
            return result
        return result.model_copy(update={"externalImageUrl": scratch.imageUrl})

    def _fetch_enabled_image_categories(self) -> set[str]:
        # Admin-portal-controlled kill switch per image category (see
        # catalog_image_flags_service.py). Fails open to "all enabled" on
        # any error -- a flags-table hiccup should never blank out images
        # that would otherwise display fine.
        all_categories = {
            "funko",
            "pokemon",
            "lego",
            "magic",
            "yugioh",
            "lorcana",
            "onepiece",
            "videogames",
        }
        try:
            payload = self._request(
                "GET",
                "/rest/v1/catalog_image_source_flags",
                params={"select": "category,enabled"},
            )
        except Exception:
            return all_categories
        if not isinstance(payload, list):
            return all_categories
        disabled = {
            str(row.get("category"))
            for row in payload
            if isinstance(row, dict) and row.get("enabled") is False
        }
        return all_categories - disabled

    def _fetch_catalog_row(self, catalog_id: str) -> dict[str, Any] | None:
        params = {
            "select": (
                "pricecharting_id,product_name,console_name,category,upc,"
                "loose_price_cents,cib_price_cents,new_price_cents,"
                "graded_price_cents,box_only_price_cents,manual_only_price_cents,"
                "currency,product_url,source_file,source_downloaded_at,"
                "updated_at,normalized_identity,"
                "source_provider,market_value_cents,low_estimate_cents,"
                "high_estimate_cents"
            ),
            "pricecharting_id": f"eq.{catalog_id}",
            "limit": "1",
        }
        payload = self._request("GET", "/rest/v1/pricecharting_catalog", params=params)
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        return row if isinstance(row, dict) else None

    def _fetch_history_rows(self, catalog_id: str, limit: int) -> list[dict[str, Any]]:
        params = {
            "select": (
                "valid_from,valid_to,is_current,source_file,source_downloaded_at,"
                "loose_price_cents,cib_price_cents,new_price_cents,"
                "graded_price_cents,box_only_price_cents,manual_only_price_cents,"
                "currency"
            ),
            "pricecharting_id": f"eq.{catalog_id}",
            "order": "valid_from.desc",
            "limit": str(limit),
        }
        payload = self._request(
            "GET",
            "/rest/v1/pricecharting_catalog_history",
            params=params,
        )
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _enrich_with_funko_image(self, result: CatalogSearchResult) -> CatalogSearchResult:
        # Funko Pop rows come from PriceCharting (real pricing, no image
        # field at all — see docs/GLOBAL_CATALOG_ARCHITECTURE.md). This
        # attaches a real photo from the static funko_pop_catalog reference
        # table (imported from the open-source funko-pop-data dataset) when
        # a confident exact-title match exists. Never overwrites an image a
        # result already has (e.g. KicksDB rows), and never guesses — no
        # match means no image, same as before.
        if result.imageUrl or not result.setName or "funko" not in result.setName.lower():
            return result
        image_url = self._fetch_funko_image(result.title)
        if image_url is None:
            return result
        return result.model_copy(update={"imageUrl": image_url})

    def _enrich_with_pokemon_image(self, result: CatalogSearchResult) -> CatalogSearchResult:
        # PriceCharting has no image data for Pokemon cards. Images come
        # from tcgplayer_pokemon_catalog, our own table imported from
        # TCGCSV (a free, daily-updated cache of TCGplayer's real product
        # catalog — see scripts/import_tcgplayer_pokemon_catalog.py).
        # Deliberately narrow, same reasoning as the Funko lookup: only
        # the handful of classic English sets in
        # _POKEMON_SET_TCGPLAYER_GROUPS, each hand-verified before being
        # added. No set mapping means no attempt — never a fuzzy guess.
        #
        # Print-variant safety: PriceCharting keeps separate priced rows
        # for the same card across print variants (e.g. Charizard Base
        # Set #4 has 5 rows — plain, [1999-2000], [1st Edition],
        # [Shadowless], [Black Dot Error]). TCGplayer/TCGCSV only
        # separately photographs a couple of those (Shadowless gets its
        # own group; some named error cards get their own product) —
        # verified live across 8 sets (Base Set, Jungle, Fossil, Team
        # Rocket, Gym Heroes, Gym Challenge, Neo Genesis, Neo Discovery),
        # see docs/GLOBAL_CATALOG_ARCHITECTURE.md. Everywhere else, one
        # card produces one photo regardless of print, so attaching it to
        # every sibling row would risk a visibly wrong print (e.g. a 1st
        # Edition stamp shown on an Unlimited row). So: (1) always try an
        # *exact* variant-specific match first; (2) if that's not
        # confirmed and this card has sibling PriceCharting rows for the
        # same set+number, suppress rather than guess; (3) otherwise (no
        # siblings — true for most cards, which only have one row) use
        # the plain match, since there's no ambiguity to get wrong.
        if result.imageUrl or not result.setName or "pokemon" not in result.setName.lower():
            return result
        group_name = _POKEMON_SET_TCGPLAYER_GROUPS.get(result.setName.strip().lower())
        if group_name is None:
            return result
        card_number = _pokemon_card_number(result.title)
        if card_number is None:
            return result

        variant_token = _pokemon_variant_token(result.title)
        exact_image_url = self._fetch_tcgplayer_exact_variant_image(
            group_name, card_number, variant_token
        )
        if exact_image_url:
            return result.model_copy(update={"imageUrl": exact_image_url})

        if self._has_sibling_pokemon_rows(result.setName, card_number, exclude_id=result.id):
            return result

        generic_image_url = self._fetch_tcgplayer_generic_image(group_name, card_number)
        if generic_image_url is None:
            return result
        return result.model_copy(update={"imageUrl": generic_image_url})

    def _fetch_tcgplayer_rows(self, group_name: str, card_number: str) -> list[dict[str, Any]]:
        params = {
            "select": "product_name,image_url,variant_tag",
            "group_name": f"eq.{group_name}",
            "card_number": f"eq.{card_number}",
        }
        payload = self._request("GET", "/rest/v1/tcgplayer_pokemon_catalog", params=params)
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _fetch_tcgplayer_exact_variant_image(
        self, group_name: str, card_number: str, variant_token: str | None
    ) -> str | None:
        if not variant_token:
            return None
        if "shadowless" in variant_token:
            for row in self._fetch_tcgplayer_rows(f"{group_name} (Shadowless)", card_number):
                image_url = row.get("image_url")
                if image_url:
                    return str(image_url)
            return None
        # Named error/misprint products (e.g. "Charizard (Black Dot
        # Error)"): only ever an exact match when every word of the
        # PriceCharting bracket tag also appears in the TCGCSV product
        # name — deliberately conservative rather than matching on
        # variant_tag=='error' alone, since a set+number could in theory
        # have more than one differently-named error product.
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

    def _fetch_tcgplayer_generic_image(self, group_name: str, card_number: str) -> str | None:
        rows = [
            row
            for row in self._fetch_tcgplayer_rows(group_name, card_number)
            if not row.get("variant_tag")
        ]
        if len(rows) != 1:
            # Zero matches, or an unexpected ambiguity within TCGCSV's own
            # data — either way, no single image we're confident in.
            return None
        image_url = rows[0].get("image_url")
        return str(image_url) if image_url else None

    def _has_sibling_pokemon_rows(
        self, set_name: str, card_number: str, *, exclude_id: str
    ) -> bool:
        # Cheap by design: an indexed eq filter on console_name narrows
        # to a handful of rows before the ilike suffix check ever runs,
        # nothing like the unindexed full-table scans this table has
        # broken on before (see _fetch_rows()'s comment).
        params = {
            "select": "pricecharting_id",
            "console_name": f"eq.{set_name}",
            "product_name": f"ilike.*#{card_number}",
            "limit": "3",
        }
        payload = self._request("GET", "/rest/v1/pricecharting_catalog", params=params)
        if not isinstance(payload, list):
            return False
        return any(
            str(row.get("pricecharting_id")) != str(exclude_id)
            for row in payload
            if isinstance(row, dict)
        )

    def _enrich_with_lego_image(self, result: CatalogSearchResult) -> CatalogSearchResult:
        # PriceCharting has no image data for LEGO sets either. Images come
        # from rebrickable_lego_catalog, our own table imported from
        # Rebrickable's free, public, no-key bulk export (28,099 real sets,
        # 100% image coverage -- see scripts/import_rebrickable_lego_
        # catalog.py). Unlike Pokemon cards, a LEGO set number is a unique
        # retail product identifier, not a card+print-run pair, so there's
        # no print-variant ambiguity to guard against here.
        #
        # There IS a different real risk, spot-checked live: LEGO has
        # reused old set numbers across unrelated product lines over the
        # decades (PriceCharting's "Roof Bricks #445" collides on number
        # with Rebrickable's unrelated "Police Units" set). Matching on
        # set number alone measured ~96% "matches" but included real false
        # positives; requiring the PriceCharting title's own words to
        # overlap with Rebrickable's matched set name as well brought that
        # down to a safe ~88% with the false positives eliminated. So:
        # never trust the number alone -- always confirm word overlap too.
        if result.imageUrl or not result.setName or "lego" not in result.setName.lower():
            return result
        base_number = _lego_set_number(result.title)
        if base_number is None:
            return result
        title_words = _lego_name_words(_lego_title_before_number(result.title))
        if not title_words:
            return result
        for row in self._fetch_lego_rows(base_number):
            candidate_words = _lego_name_words(str(row.get("name") or ""))
            if title_words & candidate_words:
                image_url = row.get("image_url")
                if image_url:
                    return result.model_copy(update={"imageUrl": str(image_url)})
        return result

    def _fetch_lego_rows(self, base_number: str) -> list[dict[str, Any]]:
        params = {
            "select": "name,image_url",
            "base_number": f"eq.{base_number}",
        }
        payload = self._request("GET", "/rest/v1/rebrickable_lego_catalog", params=params)
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _enrich_with_magic_image(self, result: CatalogSearchResult) -> CatalogSearchResult:
        # PriceCharting has no image data for Magic cards either. Images
        # come from scryfall_magic_catalog, our own table imported from
        # Scryfall's free, public, no-key bulk export -- see
        # scripts/import_scryfall_magic_catalog.py. Scryfall (not TCGCSV)
        # was chosen for Magic specifically: it's purpose-built for the
        # game and models every distinct printing -- including special
        # treatments like Showcase and Gilded Foil -- as its own card
        # object with its own collector_number, which lines up exactly
        # with the "#number" PriceCharting already embeds in these rows'
        # titles (e.g. "Cabaretti Charm [Gilded Foil] #365" -> Scryfall's
        # matching Gilded Showcase print has collector_number "365",
        # verified live). Spot-checked against a real 400-row sample:
        # once the set resolves, every card matched -- 0 misses.
        #
        # Matching is therefore number-first (safe and exact, unlike
        # LEGO/Pokemon which needed extra safety nets): normalized set
        # name + collector_number. Older/vintage rows often have no
        # number in the title at all, so those fall back to normalized
        # set name + exact card name -- but ONLY when that resolves to
        # exactly one row; an ambiguous multi-row name match (e.g. a
        # reprinted basic land with no distinguishing number) is treated
        # as no confident match rather than guessed at.
        if result.imageUrl or not result.setName or "magic" not in result.setName.lower():
            return result
        set_name = _magic_set_name_from_console(result.setName)
        if set_name is None:
            return result
        normalized_set_name = _normalize_magic_text(set_name)
        card_number = _magic_card_number(result.title)
        if card_number is not None:
            image_url = self._fetch_magic_image_by_number(normalized_set_name, card_number)
            if image_url:
                return result.model_copy(update={"imageUrl": image_url})
            return result
        card_name = _magic_card_name(result.title)
        normalized_name = _normalize_magic_text(card_name)
        if not normalized_name:
            return result
        image_url = self._fetch_magic_image_by_name(normalized_set_name, normalized_name)
        if image_url is None:
            return result
        return result.model_copy(update={"imageUrl": image_url})

    def _fetch_magic_image_by_number(self, normalized_set_name: str, card_number: str) -> str | None:
        params = {
            "select": "image_url",
            "normalized_set_name": f"eq.{normalized_set_name}",
            "collector_number": f"eq.{card_number}",
            "limit": "2",
        }
        payload = self._request("GET", "/rest/v1/scryfall_magic_catalog", params=params)
        if not isinstance(payload, list) or len(payload) != 1:
            return None
        row = payload[0]
        image_url = row.get("image_url") if isinstance(row, dict) else None
        return str(image_url) if image_url else None

    def _fetch_magic_image_by_name(self, normalized_set_name: str, normalized_name: str) -> str | None:
        params = {
            "select": "image_url",
            "normalized_set_name": f"eq.{normalized_set_name}",
            "normalized_name": f"eq.{normalized_name}",
            "limit": "2",
        }
        payload = self._request("GET", "/rest/v1/scryfall_magic_catalog", params=params)
        if not isinstance(payload, list) or len(payload) != 1:
            return None
        row = payload[0]
        image_url = row.get("image_url") if isinstance(row, dict) else None
        return str(image_url) if image_url else None

    def _enrich_with_yugioh_image(self, result: CatalogSearchResult) -> CatalogSearchResult:
        # PriceCharting has no image data for Yu-Gi-Oh cards either.
        # Images come from yugioh_catalog, our own table imported from
        # two free, public, no-key sources -- YGOPRODeck (primary) and
        # TCGCSV (fallback) -- see scripts/import_ygoprodeck_catalog.py
        # and scripts/import_tcgcsv_yugioh_catalog.py.
        #
        # Unlike Pokemon/Magic/LEGO, this needs no per-set resolution at
        # all: PriceCharting's Yu-Gi-Oh titles already embed Yu-Gi-Oh's
        # own globally unique "set code" (e.g. "Where Arf Thou?
        # SD40-JP033"), and both source databases key printings by that
        # exact same code -- so the code alone is the entire matching
        # problem, verified live against a real 566-row sample (95% of
        # rows with an extractable code got a real image). Neither source
        # has meaningful Japanese-exclusive (OCG) set coverage -- a real,
        # confirmed gap, not something this enrichment tries to paper
        # over.
        if result.imageUrl or not result.setName or "yugioh" not in result.setName.lower():
            return result
        set_code = _yugioh_set_code(result.title)
        if set_code is None:
            return result
        image_url = self._fetch_yugioh_image(set_code)
        if image_url is None:
            return result
        return result.model_copy(update={"imageUrl": image_url})

    def _fetch_yugioh_image(self, set_code: str) -> str | None:
        params = {
            "select": "image_url",
            "set_code": f"eq.{set_code}",
            "limit": "1",
        }
        payload = self._request("GET", "/rest/v1/yugioh_catalog", params=params)
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        image_url = row.get("image_url") if isinstance(row, dict) else None
        return str(image_url) if image_url else None

    def _enrich_with_lorcana_image(self, result: CatalogSearchResult) -> CatalogSearchResult:
        # PriceCharting has no image data for Lorcana cards either. Images
        # come from lorcana_catalog, our own table imported from two free,
        # public, no-key sources -- lorcana-api.com (primary; images
        # hosted on the official Ravensburger publisher CDN, verified
        # live) and Lorcast (fallback) -- see scripts/import_lorcana_
        # api_catalog.py and scripts/import_lorcast_catalog.py.
        #
        # Lorcana card numbers are unique only within a set (not globally,
        # unlike Yu-Gi-Oh's set codes), so the lookup key is normalized
        # set name + card number -- same shape as Magic's number-based
        # match, and safe the same way: no reused-number risk was found
        # for Lorcana (unlike LEGO), verified live against a real 500-row
        # sample (99%+ of numbered rows matched). The one confirmed real
        # gap is PriceCharting's "Lorcana Promo" console_name, which
        # doesn't resolve to one specific promo set (Lorcana has several)
        # -- left unmatched rather than guessed at.
        if result.imageUrl or not result.setName or "lorcana" not in result.setName.lower():
            return result
        set_name = _lorcana_set_name_from_console(result.setName)
        if set_name is None:
            return result
        card_number = _lego_set_number(result.title)
        if card_number is None:
            return result
        image_url = self._fetch_lorcana_image(_normalize_magic_text(set_name), card_number)
        if image_url is None:
            return result
        return result.model_copy(update={"imageUrl": image_url})

    def _fetch_lorcana_image(self, normalized_set_name: str, card_number: str) -> str | None:
        params = {
            "select": "image_url",
            "normalized_set_name": f"eq.{normalized_set_name}",
            "card_number": f"eq.{card_number}",
            "limit": "1",
        }
        payload = self._request("GET", "/rest/v1/lorcana_catalog", params=params)
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        image_url = row.get("image_url") if isinstance(row, dict) else None
        return str(image_url) if image_url else None

    def _enrich_with_onepiece_image(self, result: CatalogSearchResult) -> CatalogSearchResult:
        # PriceCharting has no image data for One Piece Card Game cards
        # either. Images come from one_piece_catalog, our own table
        # imported from optcgapi.com's free, public, no-key bulk export
        # -- see scripts/import_onepiece_catalog.py.
        #
        # This is the most fragmented card game matched so far: unlike
        # Yu-Gi-Oh's globally unique set codes, One Piece promo reprints
        # routinely reuse the base card's set code (verified live: 40% of
        # codes map to more than one card -- championship prizes,
        # tournament packs, box toppers). So: a PriceCharting row with no
        # bracket tag only matches when exactly one "plain" (no variant
        # suffix) row exists for its code; a row with a bracket tag (e.g.
        # "[Alternate Art]") requires a word-overlap match against a
        # non-plain row's name, ignoring short set-code-shaped tokens
        # (e.g. "op07", "prb01") that would otherwise cause false
        # matches. Anything else is left unmatched. Spot-checked against
        # a real 500-row sample: 63% real match rate -- the remaining gap
        # is genuinely unavailable data (Japanese-exclusive prints, DON!!
        # cards which have no set code at all, and specific tournament-
        # only promos optcgapi doesn't carry), not a guessable pattern.
        if result.imageUrl or not result.setName or "one piece" not in result.setName.lower():
            return result
        set_code = _onepiece_set_code(result.title)
        if set_code is None:
            return result
        rows = self._fetch_onepiece_rows(set_code)
        if not rows:
            return result
        variant_token = _pokemon_variant_token(result.title)
        if variant_token is None:
            plain_rows = [row for row in rows if row.get("is_plain")]
            if len(plain_rows) != 1:
                return result
            image_url = plain_rows[0].get("image_url")
            return (
                result.model_copy(update={"imageUrl": str(image_url)})
                if image_url
                else result
            )
        variant_words = _onepiece_meaningful_words(variant_token)
        if not variant_words:
            return result
        # Every word in the PriceCharting tag must appear in the
        # candidate's name (not just any overlap) -- a looser "any word
        # in common" check was tried first and produced real wrong
        # matches: e.g. "[Championship 2024 Top Player]" and
        # "[Championship 2024 Finalist]" share "championship"/"2024" and
        # would both match the same candidate under intersection-only
        # matching. And if more than one candidate satisfies the subset
        # check, that's still ambiguous -- never guess which one.
        matches = [
            row
            for row in rows
            if not row.get("is_plain")
            and variant_words.issubset(_onepiece_meaningful_words(str(row.get("card_name") or "")))
        ]
        if len(matches) != 1:
            return result
        image_url = matches[0].get("image_url")
        if image_url is None:
            return result
        return result.model_copy(update={"imageUrl": str(image_url)})

    def _fetch_onepiece_rows(self, card_set_id: str) -> list[dict[str, Any]]:
        params = {
            "select": "card_name,is_plain,image_url",
            "card_set_id": f"eq.{card_set_id}",
        }
        payload = self._request("GET", "/rest/v1/one_piece_catalog", params=params)
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _enrich_with_video_games_image(
        self, result: CatalogSearchResult
    ) -> CatalogSearchResult:
        # Unlike every other category above, PriceCharting's video-games
        # rows have no distinguishing "video games" keyword anywhere in
        # category/setName to gate on the same way "pokemon" in setName
        # etc. works elsewhere -- verified against real PriceCharting CSV
        # data (scripts/import_pricecharting_catalog.py): the video-games
        # export has no genre/category column at all, so `category` falls
        # back to `console_name` (e.g. "Nintendo 64", "Playstation 4"),
        # same as setName. So the gate here IS the platform mapping
        # itself: _video_game_rawg_platform only recognizes real console
        # names, and returns None for every other category's setName
        # (e.g. "Magic Streets of New Capenna", "LEGO Space") -- no
        # mapping means no attempt, same "no match beats a wrong match"
        # philosophy as every other category's unmapped-set/unmapped-
        # platform behavior.
        #
        # Also unlike every other category, RAWG's 500,000+ game catalog
        # is far too large to bulk-import, so this is a live lookup +
        # write-through cache (rawg_video_game_cache -- see
        # database/migrations/20260819_create_rawg_video_game_cache.sql)
        # instead of a pre-imported reference table: a cache hit (match
        # or confirmed no-match) never calls RAWG again; a cache miss
        # calls RAWG's search endpoint once, applies the same
        # exact-title + exact-platform matching discipline as every
        # other category (zero or ambiguous matches -> suppress, never
        # guess), and writes the outcome back so the same item is never
        # re-queried.
        if result.imageUrl or not result.setName:
            return result
        rawg_platform = _video_game_rawg_platform(result.setName)
        if rawg_platform is None:
            return result
        base_title = _video_game_base_title(result.title)
        if not base_title:
            return result

        lookup_key = _video_game_cache_key(base_title, rawg_platform)
        cached = self._fetch_video_game_cache(lookup_key)
        if cached is not None:
            image_url = cached.get("image_url") if cached.get("matched") else None
            return (
                result.model_copy(update={"imageUrl": str(image_url)})
                if image_url
                else result
            )

        payload = self._fetch_rawg_search(base_title)
        if payload is None:
            # Network/HTTP failure talking to RAWG -- never cached as a
            # negative match, just try again on the next lookup.
            return result

        raw_results = payload.get("results") if isinstance(payload, dict) else None
        candidates = [row for row in raw_results or [] if isinstance(row, dict)]
        platform_matches = [
            row for row in candidates if _rawg_row_has_platform(row, rawg_platform)
        ]
        exact_matches = [
            row
            for row in platform_matches
            if str(row.get("name") or "").strip().lower() == base_title.lower()
        ]

        if len(exact_matches) == 1:
            game = exact_matches[0]
            image_url = _clean(game.get("background_image"))
            slug = _clean(game.get("slug"))
        else:
            image_url = None
            slug = None

        self._write_video_game_cache(
            lookup_key,
            matched=bool(image_url),
            image_url=image_url,
            rawg_slug=slug,
        )
        if image_url is None:
            return result
        return result.model_copy(update={"imageUrl": image_url})

    def _fetch_video_game_cache(self, lookup_key: str) -> dict[str, Any] | None:
        params = {
            "select": "matched,image_url,rawg_slug",
            "lookup_key": f"eq.{lookup_key}",
            "limit": "1",
        }
        payload = self._request("GET", "/rest/v1/rawg_video_game_cache", params=params)
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        return row if isinstance(row, dict) else None

    def _write_video_game_cache(
        self,
        lookup_key: str,
        *,
        matched: bool,
        image_url: str | None,
        rawg_slug: str | None,
    ) -> None:
        try:
            self._request(
                "POST",
                "/rest/v1/rawg_video_game_cache",
                params={"on_conflict": "lookup_key"},
                json_payload={
                    "lookup_key": lookup_key,
                    "matched": matched,
                    "image_url": image_url,
                    "rawg_slug": rawg_slug,
                    "checked_at": datetime.utcnow().isoformat() + "Z",
                },
                extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
        except CatalogSearchError:
            # A failed cache write just means we ask RAWG again next
            # time -- never let it break the response the user is
            # waiting on.
            pass

    def _fetch_rawg_search(self, title: str) -> dict[str, Any] | None:
        api_key = settings.rawg_api_key.strip()
        if not api_key:
            return None
        client = self.client or httpx.Client(timeout=self.timeout_seconds)
        should_close = self.client is None
        try:
            response = client.get(
                f"{settings.rawg_api_base.rstrip('/')}/games",
                params={"search": title, "key": api_key, "page_size": "5"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        finally:
            if should_close:
                client.close()
        return payload if isinstance(payload, dict) else None

    def _fetch_funko_image(self, product_title: str) -> str | None:
        lookup_title = _funko_lookup_title(product_title)
        if not lookup_title:
            return None
        params = {
            "select": "image_url,series",
            "normalized_title": f"eq.{lookup_title}",
            "limit": "10",
        }
        payload = self._request("GET", "/rest/v1/funko_pop_catalog", params=params)
        if not isinstance(payload, list) or not payload:
            return None
        return select_best_funko_image(payload)

    def _fetch_kicksdb_rows(self, query: str, limit: int) -> list[dict[str, Any]]:
        # Mirrors _fetch_rows()'s RPC-based ranking (see that method's
        # comment for why a plain REST ORDER BY isn't safe here either).
        # kicksdb_catalog is ~11K rows, well under the size where the
        # adaptive broad-query fallback used for pricecharting_catalog
        # becomes necessary.
        payload = self._request(
            "POST",
            "/rest/v1/rpc/search_kicksdb_catalog",
            json_payload={"search_query": query, "result_limit": limit},
        )
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _fetch_kicksdb_catalog_row(self, catalog_id: str) -> dict[str, Any] | None:
        params = {
            "select": (
                "kicksdb_id,title,brand,model,gender,product_type,category,"
                "secondary_category,image_url,rank,release_date,currency,"
                "min_price_cents,max_price_cents,avg_price_cents,product_url,"
                "sku,updated_at"
            ),
            "kicksdb_id": f"eq.{catalog_id}",
            "limit": "1",
        }
        payload = self._request("GET", "/rest/v1/kicksdb_catalog", params=params)
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        return row if isinstance(row, dict) else None

    def _fetch_kicksdb_history_rows(
        self, catalog_id: str, limit: int
    ) -> list[dict[str, Any]]:
        params = {
            "select": (
                "valid_from,valid_to,is_current,source_downloaded_at,"
                "min_price_cents,max_price_cents,avg_price_cents,currency"
            ),
            "kicksdb_id": f"eq.{catalog_id}",
            "order": "valid_from.desc",
            "limit": str(limit),
        }
        payload = self._request(
            "GET",
            "/rest/v1/kicksdb_catalog_history",
            params=params,
        )
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

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
        client = self.client or httpx.Client(timeout=self.timeout_seconds)
        should_close = self.client is None
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
            raise CatalogSearchError("Catalog search request failed.") from error
        finally:
            if should_close:
                client.close()


_PROVIDER_DISPLAY_NAMES = {
    "pricecharting_import": "PriceCharting",
    "pricecharting": "PriceCharting",
    "kicksdb": "KicksDB",
    "tcgplayer": "TCGPlayer",
    "ebay": "eBay",
}


def _source_display_name(row: dict[str, Any]) -> str:
    provider = str(row.get("source_provider") or "").strip().lower()
    return _PROVIDER_DISPLAY_NAMES.get(provider, "PriceCharting")


def _row_to_result(row: dict[str, Any], query: str) -> CatalogSearchResult:
    pricing = _pricing_from_row(row)
    source = _source_display_name(row)
    return CatalogSearchResult(
        id=str(row.get("pricecharting_id") or ""),
        title=str(row.get("product_name") or "Catalog item"),
        category=str(row.get("category") or row.get("console_name") or "Catalog"),
        source=source,
        setName=_clean(row.get("console_name")),
        identifier=_clean(row.get("upc")),
        productUrl=_clean(row.get("product_url")),
        sourceFile=_clean(row.get("source_file")),
        confidence=_match_confidence(row, query),
        attribution=f"Pricing data by {source}",
        lastUpdated=_latest_timestamp(row),
        imageUrl=None,
        pricing=pricing,
    )


def _pricing_from_row(row: dict[str, Any]) -> CatalogSearchPricing:
    loose = _cents_to_units(row.get("loose_price_cents"))
    cib = _cents_to_units(row.get("cib_price_cents"))
    new = _cents_to_units(row.get("new_price_cents"))
    graded = _cents_to_units(row.get("graded_price_cents"))
    prices = [price for price in [loose, cib, new, graded] if price is not None and price > 0]
    market_value = loose or cib or new or graded
    low = min(prices) if prices else None
    high = max(prices) if prices else None

    if market_value is None:
        # Scan-derived rows (source_kind='scan_derived') don't populate the
        # PriceCharting-specific price tiers above — fall back to the
        # provider-neutral market_value_cents/low/high_estimate_cents columns
        # instead (see docs/GLOBAL_CATALOG_ARCHITECTURE.md).
        market_value = _cents_to_units(row.get("market_value_cents"))
        low = _cents_to_units(row.get("low_estimate_cents"))
        high = _cents_to_units(row.get("high_estimate_cents"))

    return CatalogSearchPricing(
        currency=str(row.get("currency") or "USD").upper(),
        marketValue=market_value,
        lowEstimate=low,
        highEstimate=high,
        loosePrice=loose,
        cibPrice=cib,
        newPrice=new,
        gradedPrice=graded,
    )


def _kicksdb_row_to_result(row: dict[str, Any], query: str) -> CatalogSearchResult:
    pricing = _kicksdb_pricing_from_row(row)
    return CatalogSearchResult(
        id=str(row.get("kicksdb_id") or ""),
        title=str(row.get("title") or "Catalog item"),
        category=str(row.get("category") or row.get("product_type") or "Sneakers"),
        source="KicksDB",
        setName=_clean(row.get("brand")),
        identifier=_clean(row.get("sku")),
        productUrl=_clean(row.get("product_url")),
        sourceFile=None,
        confidence=_kicksdb_match_confidence(row, query),
        attribution="Pricing data by KicksDB",
        lastUpdated=_clean(row.get("updated_at")) or (datetime.utcnow().isoformat() + "Z"),
        imageUrl=_clean(row.get("image_url")),
        pricing=pricing,
    )


def _kicksdb_pricing_from_row(row: dict[str, Any]) -> CatalogSearchPricing:
    return CatalogSearchPricing(
        currency=str(row.get("currency") or "USD").upper(),
        marketValue=_cents_to_units(row.get("avg_price_cents")),
        lowEstimate=_cents_to_units(row.get("min_price_cents")),
        highEstimate=_cents_to_units(row.get("max_price_cents")),
    )


def _kicksdb_match_confidence(row: dict[str, Any], query: str) -> float:
    score = _kicksdb_match_score(row, query)
    if score >= 100:
        return 0.96
    if score >= 80:
        return 0.90
    if score >= 55:
        return 0.78
    return 0.62


def _kicksdb_match_score(row: dict[str, Any], query: str) -> int:
    title = str(row.get("title") or "").lower()
    brand = str(row.get("brand") or "").lower()
    model = str(row.get("model") or "").lower()
    sku = str(row.get("sku") or "").lower()
    if query == title or (sku and query == sku):
        return 110
    if title.startswith(query):
        return 95
    if query in title:
        return 80
    if query in brand or query in model:
        return 55
    return 25


def _kicksdb_history_row_to_point(row: dict[str, Any]) -> CatalogHistoryPoint:
    return CatalogHistoryPoint(
        validFrom=str(row.get("valid_from") or ""),
        validTo=_clean(row.get("valid_to")),
        isCurrent=bool(row.get("is_current")),
        sourceFile=None,
        sourceDownloadedAt=_clean(row.get("source_downloaded_at")),
        pricing=_kicksdb_pricing_from_row(row),
    )


def _history_row_to_point(row: dict[str, Any]) -> CatalogHistoryPoint:
    return CatalogHistoryPoint(
        validFrom=str(row.get("valid_from") or ""),
        validTo=_clean(row.get("valid_to")),
        isCurrent=bool(row.get("is_current")),
        sourceFile=_clean(row.get("source_file")),
        sourceDownloadedAt=_clean(row.get("source_downloaded_at")),
        pricing=_pricing_from_row(row),
    )


def _rank_rows(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (-_match_score(row, query), str(row.get("product_name") or "")))


def _match_confidence(row: dict[str, Any], query: str) -> float:
    score = _match_score(row, query)
    if score >= 100:
        return 0.96
    if score >= 80:
        return 0.90
    if score >= 55:
        return 0.78
    return 0.62


def _match_score(row: dict[str, Any], query: str) -> int:
    product = str(row.get("product_name") or "").lower()
    console = str(row.get("console_name") or "").lower()
    category = str(row.get("category") or "").lower()
    upc = str(row.get("upc") or "").lower()
    identity = str(row.get("normalized_identity") or "").lower()
    if query == product or query == upc:
        return 110
    if product.startswith(query):
        return 95
    if query in product:
        return 80
    if query in identity:
        return 70
    if query in console or query in category:
        return 55
    return 25


def _normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())


# Verified live against Rebrickable's free, no-key bulk export
# (rebrickable.com/downloads, sets.csv.gz) before being added -- 28,099
# real LEGO sets, 100% image coverage. LEGO set numbers are unique retail
# product identifiers (unlike Pokemon cards, no print-variant ambiguity),
# but the number alone isn't safe to match on: LEGO has reused old set
# numbers across unrelated product lines over the decades, spot-checked
# live (PriceCharting's "Roof Bricks #445" collides on number with
# Rebrickable's unrelated "Police Units" set). Requiring the PriceCharting
# title's own words to overlap with Rebrickable's matched name eliminated
# those false positives while keeping ~88% real coverage.
_LEGO_SET_NUMBER_RE = re.compile(r"#(\d+)")
_LEGO_STOPWORDS = {"the", "a", "an", "of", "and", "set", "lego"}


def _lego_set_number(product_title: str) -> str | None:
    match = _LEGO_SET_NUMBER_RE.search(product_title)
    if not match:
        return None
    return match.group(1).lstrip("0") or "0"


def _lego_title_before_number(product_title: str) -> str:
    match = _LEGO_SET_NUMBER_RE.search(product_title)
    return product_title[: match.start()].strip() if match else product_title.strip()


def _lego_name_words(text: str) -> set[str]:
    return {word for word in _normalize_variant_words(text) if word not in _LEGO_STOPWORDS}


# Verified live against Scryfall's free bulk export (api.scryfall.com/
# bulk-data -> default_cards) before being added -- 116,712 real English
# cards. See scripts/import_scryfall_magic_catalog.py and
# _enrich_with_magic_image for the full reasoning. This normalization
# (strip apostrophes, collapse all other punctuation to spaces) is applied
# to BOTH sides of every match -- PriceCharting's console_name/product_name
# here, and Scryfall's set_name/name at import time -- so it MUST stay
# identical between the two; the import script imports this function
# directly rather than re-implementing it, specifically to prevent drift.
_MAGIC_CARD_NUMBER_RE = re.compile(r"#(\S+)")
_MAGIC_BRACKET_TAG_RE = re.compile(r"\s*\[[^\]]*\]")
_MAGIC_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")


def _normalize_magic_text(text: str) -> str:
    text = text.replace("'", "").replace("’", "")
    text = _MAGIC_NON_ALNUM_RE.sub(" ", text.lower())
    return " ".join(text.split())


def _magic_set_name_from_console(console_name: str) -> str | None:
    # PriceCharting's Magic console_name is always "Magic <set name>"
    # (e.g. "Magic Streets of New Capenna") -- verified against a real
    # 400-row sample, no exceptions found.
    text = console_name.strip()
    if not text.lower().startswith("magic "):
        return None
    return text[len("Magic "):].strip() or None


def _magic_card_number(product_title: str) -> str | None:
    match = _MAGIC_CARD_NUMBER_RE.search(product_title)
    return match.group(1) if match else None


def _magic_card_name(product_title: str) -> str:
    text = _MAGIC_CARD_NUMBER_RE.sub("", product_title)
    text = _MAGIC_BRACKET_TAG_RE.sub("", text)
    return text.strip()


# Yu-Gi-Oh's own globally unique set-code convention (e.g. "LOB-027",
# "SD40-JP033", "INFO-JP043") -- verified live against a real 600-row
# PriceCharting sample (98.4% of real card rows had an extractable code)
# before being added. Sealed products ("Booster Pack", "Booster Box")
# have no code and are correctly left unmatched by this pattern.
_YUGIOH_SET_CODE_RE = re.compile(r"\b([A-Z0-9]{2,6}-[A-Z]{0,3}\d{2,4})\s*$")


def _yugioh_set_code(product_title: str) -> str | None:
    match = _YUGIOH_SET_CODE_RE.search(product_title)
    return match.group(1) if match else None


def _lorcana_set_name_from_console(console_name: str) -> str | None:
    # PriceCharting's Lorcana console_name is always "Lorcana <set name>"
    # (e.g. "Lorcana Attack of the Vine") -- verified against a real
    # 500-row sample, no exceptions found.
    text = console_name.strip()
    if not text.lower().startswith("lorcana "):
        return None
    return text[len("Lorcana "):].strip() or None


# Bandai's own One Piece Card Game set-code convention (e.g. "OP07-082",
# "ST21-012", "P-029") -- verified live against a real 500-row
# PriceCharting sample. Deliberately 1-4 letters (not 2-4): single-letter
# promo codes like "P-029" are common and were missed by an earlier,
# stricter version of this pattern during development.
_ONEPIECE_SET_CODE_RE = re.compile(r"([A-Z]{1,4}\d{0,2}-[A-Z]{0,3}\d{2,4})\s*$")
# Short tokens that look like a set-code fragment (e.g. "op07", "prb01")
# rather than a real descriptive word -- PriceCharting's bracket tags
# sometimes repeat the set code itself (e.g. "[Alternate Art PRB01]"),
# and treating that as a real word to match against would risk a false
# positive against any card that happens to share it.
_ONEPIECE_CODE_FRAGMENT_RE = re.compile(r"^[a-z]{1,4}\d{0,3}$")


def _onepiece_set_code(product_title: str) -> str | None:
    match = _ONEPIECE_SET_CODE_RE.search(product_title)
    return match.group(1) if match else None


def _onepiece_meaningful_words(text: str) -> set[str]:
    words = _normalize_variant_words(text)
    return {word for word in words if not _ONEPIECE_CODE_FRAGMENT_RE.match(word)}


# Verified live against real TCGCSV data (tcgcsv.com/tcgplayer/3/groups)
# before being added -- each key confirmed to have a matching TCGplayer
# group with real product photos. Keys are PriceCharting's exact
# console_name values, lowercased; values are TCGCSV's exact group_name
# values (as imported into tcgplayer_pokemon_catalog). Deliberately small
# and hand-maintained, same reasoning as the Funko lookup: this only
# covers a few classic English sets rather than guessing at a broader
# mapping.
_POKEMON_SET_TCGPLAYER_GROUPS: dict[str, str] = {
    "pokemon base set": "Base Set",
    "pokemon jungle": "Jungle",
    "pokemon fossil": "Fossil",
    "pokemon base set 2": "Base Set 2",
    "pokemon team rocket": "Team Rocket",
}
_POKEMON_CARD_NUMBER_RE = re.compile(r"#(\w+)")
_POKEMON_BRACKET_TAG_RE = re.compile(r"\[([^\]]*)\]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")


def _pokemon_card_number(product_title: str) -> str | None:
    match = _POKEMON_CARD_NUMBER_RE.search(product_title)
    return match.group(1) if match else None


def _pokemon_variant_token(product_title: str) -> str | None:
    # PriceCharting's print-variant tag, e.g. "Charizard [Shadowless] #4"
    # -> "shadowless". Used only to look for an *exact* TCGCSV match
    # (see _fetch_tcgplayer_exact_variant_image) -- never to fabricate an
    # image on its own.
    match = _POKEMON_BRACKET_TAG_RE.search(product_title)
    if not match:
        return None
    token = match.group(1).strip().lower()
    return token or None


def _normalize_variant_words(text: str) -> set[str]:
    cleaned = _NON_ALNUM_RE.sub(" ", text.lower())
    return {word for word in cleaned.split() if word}


_FUNKO_BRACKET_TAG_RE = re.compile(r"\[[^\]]*\]")
_FUNKO_FIGURE_NUMBER_RE = re.compile(r"#\S+")
_FUNKO_YEAR_RE = re.compile(r"\(\d{4}\)")
_FUNKO_SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def _funko_lookup_title(product_title: str) -> str:
    # Conservative, deterministic normalization only — no fuzzy matching.
    # PriceCharting names look like "13th Battalion Trooper #645" or
    # "Guardians Of The Galaxy [Funko Pop] #12 (2019)"; the community
    # dataset's titles are the bare character/product name ("13th Battalion
    # Trooper"). Anything that doesn't reduce to an exact match after this
    # normalization is left without an image rather than guessed at.
    text = product_title.translate(_FUNKO_SMART_QUOTES)
    text = _FUNKO_BRACKET_TAG_RE.sub("", text)
    text = _FUNKO_FIGURE_NUMBER_RE.sub("", text)
    text = _FUNKO_YEAR_RE.sub("", text)
    return " ".join(text.strip().lower().split())


def select_best_funko_image(candidate_rows: list[dict[str, Any]]) -> str | None:
    # Shared by catalog_search_service (mobile/public search) and
    # admin_catalog_service (admin catalog browse) so both pick the same
    # image for the same title, rather than reimplementing this twice.
    # Prefer an actual Pop! vinyl figure entry over pins/apparel/other merch
    # that happens to share the same character name.
    candidates = [row for row in candidate_rows if isinstance(row, dict) and row.get("image_url")]
    if not candidates:
        return None
    for row in candidates:
        series = row.get("series") or []
        series_text = " ".join(str(s) for s in series).lower()
        if "pop!" in series_text and not any(
            bad in series_text for bad in ("pin", "apparel", "tee", "sticker", "keychain")
        ):
            return str(row["image_url"])
    return str(candidates[0]["image_url"])


# Hand-verified against RAWG's own platform names (api.rawg.io/api/platforms)
# before being added -- deliberately small, mainstream-only, same reasoning
# as _POKEMON_SET_TCGPLAYER_GROUPS: no mapping means no RAWG attempt at all
# for that row, never a fuzzy platform guess. Keys are PriceCharting's
# console_name values (region prefix stripped, lowercased); values are
# RAWG's exact platform display names.
_VIDEO_GAME_PLATFORM_RAWG_MAP: dict[str, str] = {
    "playstation 5": "PlayStation 5",
    "playstation 4": "PlayStation 4",
    "playstation 3": "PlayStation 3",
    "playstation 2": "PlayStation 2",
    "playstation vita": "PlayStation Vita",
    "playstation": "PlayStation",
    "xbox series x": "Xbox Series S/X",
    "xbox one": "Xbox One",
    "xbox 360": "Xbox 360",
    "xbox": "Xbox",
    "nintendo switch": "Nintendo Switch",
    "wii u": "Wii U",
    "wii": "Wii",
    "gamecube": "GameCube",
    "nintendo 64": "Nintendo 64",
    "super nintendo": "SNES",
    "snes": "SNES",
    "nintendo": "NES",
    "nes": "NES",
    "nintendo 3ds": "3DS",
    "nintendo ds": "DS",
    "gameboy advance": "Game Boy Advance",
    "gameboy color": "Game Boy Color",
    "gameboy": "Game Boy",
    "psp": "PSP",
    "pc": "PC",
}

# PriceCharting console_name values carry an optional region prefix ahead of
# the platform name itself (e.g. "PAL Playstation 3", "JP Playstation") --
# stripped defensively (case-insensitively) before the platform lookup
# above, covering every region tag observed plus the common
# NTSC-J/NTSC-U/AU/EU variants.
_VIDEO_GAME_REGION_PREFIX_RE = re.compile(
    r"^(?:PAL|JP|NTSC-J|NTSC-U|AU|EU|US)\s+", re.IGNORECASE
)
_VIDEO_GAME_BRACKET_TAG_RE = re.compile(r"\s*\[[^\]]*\]")


def _video_game_rawg_platform(console_name: str) -> str | None:
    stripped = _VIDEO_GAME_REGION_PREFIX_RE.sub("", console_name.strip())
    return _VIDEO_GAME_PLATFORM_RAWG_MAP.get(stripped.strip().lower())


def _video_game_base_title(product_title: str) -> str:
    # Strip a trailing bracket tag (e.g. "[Collector's Edition]",
    # "[Demonstration Disc]") the same way other categories handle bracket
    # variants -- RAWG doesn't carry "special edition" as its own title, so
    # the base title is what's searched and matched against.
    text = _VIDEO_GAME_BRACKET_TAG_RE.sub("", product_title)
    return " ".join(text.strip().split())


def _video_game_cache_key(base_title: str, rawg_platform: str) -> str:
    return f"{base_title.strip().lower()}::{rawg_platform.strip().lower()}"


def _rawg_row_has_platform(row: dict[str, Any], rawg_platform: str) -> bool:
    platforms = row.get("platforms")
    if not isinstance(platforms, list):
        return False
    for entry in platforms:
        if not isinstance(entry, dict):
            continue
        platform = entry.get("platform")
        if isinstance(platform, dict) and str(platform.get("name") or "") == rawg_platform:
            return True
    return False


def _cents_to_units(value: Any) -> float | None:
    try:
        if value is None:
            return None
        cents = int(value)
    except (TypeError, ValueError):
        return None
    if cents <= 0:
        return None
    return round(cents / 100, 2)


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _latest_timestamp(row: dict[str, Any]) -> str | None:
    for key in ("source_downloaded_at", "updated_at"):
        value = _clean(row.get(key))
        if value:
            return value
    return datetime.utcnow().isoformat() + "Z"
