from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.config import settings
from app.services.pricing.currency_conversion import (
    SUPPORTED_DISPLAY_CURRENCIES,
    _exchange_rate,
    normalize_display_currency,
)
from app.services.pricing.ebay_listing_service import (
    EbayListingService,
    ebay_marketplace_for_currency,
)
from app.services.pricing.pricecharting_listing_service import (
    PriceChartingListingService,
)
from app.schemas.search import (
    CatalogDetailResponse,
    CatalogHistoryPoint,
    CatalogSearchPricing,
    MarketplaceListing,
    CatalogSearchResponse,
    CatalogSearchResult,
)


class CatalogSearchError(Exception):
    """Raised when catalog search cannot be completed."""


class CatalogItemNotFoundError(CatalogSearchError):
    """Raised when a catalog item cannot be found."""


# Single source of truth for pricecharting_catalog's category-group
# taxonomy -- shared by the admin Catalog products screen
# (admin_catalog_service.py, which imports these) and the public/mobile
# Discover search below. Moved here (rather than defined in
# admin_catalog_service.py) because admin_catalog_service already imports
# FROM this module -- keeping the taxonomy here avoids a circular import.
#
# pricecharting_catalog's raw `category` column is far too granular for a
# dropdown ("Basketball Cards 2019 Panini Donruss Optic", not "Sports
# Cards") -- there's no separate coarse-category column, so these groups
# are keyword sets or'd together against the same raw column. KicksDB has
# no equivalent taxonomy defined anywhere in this system.
#
# video-games is deliberately absent from this dict: PriceCharting's
# video-games rows use `category` for a real per-game genre ("Action &
# Adventure", "FPS", "RPG", ...), not a fixed small taxonomy like every
# other group here -- confirmed live against real rows (31 distinct genre
# values on Playstation 4 alone). Video Games is filtered separately, via
# PRICECHARTING_PLATFORM_GROUPS below, against the precomputed
# platform_group column instead (see
# 20260820_add_platform_group_step1_schema.sql) -- an exact-match filter
# on an indexed column, not a runtime ilike-OR (a console_name-based
# ilike-OR filter was tried and reverted for timing out at this scale,
# re-confirmed live: 57014 statement timeout even with an index on
# console_name).
PRICECHARTING_CATEGORY_GROUPS: dict[str, list[str]] = {
    "sports-cards": ["Baseball", "Basketball", "Football", "Hockey", "Soccer"],
    # "One Piece" was missing even though the catalog carries thousands of
    # its cards (categories "One Piece", "One Piece Card", "One Piece
    # Japanese Card"). Filtering to trading cards therefore *excluded* the
    # game entirely: a One Piece search fell through to fuzzy matches in the
    # other games, returning "Fluffy Berry" Pokemon cards for "Luffy" and
    # YuGiOh cards for the set code "OP01".
    "trading-card-games": ["Magic", "Pokemon", "Yugioh", "Lorcana", "One Piece"],
    "comics": ["Comic"],
    "funko-pops": ["Funko"],
    "lego-sets": ["Lego"],
    "coins": ["Coin"],
}

# Video Games platform buckets -- a SEPARATE dict from
# PRICECHARTING_CATEGORY_GROUPS because the filtering mechanism differs:
# these match the precomputed platform_group column (exact equality, see
# compute_platform_group() in 20260820_add_platform_group_step1_schema.sql
# and its Python mirror in scripts/import_pricecharting_catalog.py), not an
# ilike-OR against `category`. Keys double as the category_group value
# callers send; the values here are display labels only (the actual
# matching logic lives in the SQL function, kept in one place).
PRICECHARTING_PLATFORM_GROUPS: dict[str, str] = {
    "playstation": "PlayStation",
    "xbox": "Xbox",
    "nintendo": "Nintendo",
    "sega": "Sega",
    "atari": "Atari",
    "pc": "PC",
    "retro-other": "Other retro",
}

# Video Games as a selectable top-level category_group value -- distinct
# from PRICECHARTING_CATEGORY_GROUPS/PRICECHARTING_PLATFORM_GROUPS above
# because, unlike every other category, "Video Games with no platform
# picked" still needs to mean something (any video game, any platform) --
# see ANY_PLATFORM_GROUP below.
PRICECHARTING_VIDEO_GAMES_CATEGORY_KEY = "video-games"

# Sneakers are the one selectable category that lives entirely in
# kicksdb_catalog rather than pricecharting_catalog, so it is not part of
# PRICECHARTING_CATEGORY_GROUPS -- resolving it through the PriceCharting
# taxonomy would return nothing. Matches kSneakersCategoryKey in the
# mobile app's search screen; the two must stay in sync.
SNEAKERS_CATEGORY_KEY = "sneakers"

# Sentinel passed as platform_group_filter to mean "any recognized video-
# game platform" (platform_group is not null) rather than an exact match --
# see 20260821_add_any_platform_sentinel_to_search_pricecharting_catalog.sql.
# Chosen so it can never collide with a real platform_group value (those
# are all short lowercase-hyphen keys like 'playstation'/'retro-other').
ANY_PLATFORM_GROUP = "__any_platform__"

# Subcategory drill-downs within a top-level category -- only categories
# with a real second taxonomy dimension get an entry here. Splits the
# combined keyword lists in PRICECHARTING_CATEGORY_GROUPS into individually
# selectable subcategories; Video Games' subcategories are
# PRICECHARTING_PLATFORM_GROUPS itself (handled separately in
# resolve_category_group_filters, not duplicated here). Comics/Funko Pops/
# Lego Sets/Coins/Sneakers have exactly one flat bucket each -- nothing to
# drill into, so they're deliberately absent.
PRICECHARTING_SUBCATEGORY_GROUPS: dict[str, dict[str, list[str]]] = {
    "sports-cards": {
        "baseball": ["Baseball"],
        "basketball": ["Basketball"],
        "football": ["Football"],
        "hockey": ["Hockey"],
        "soccer": ["Soccer"],
    },
    "trading-card-games": {
        "magic": ["Magic"],
        "pokemon": ["Pokemon"],
        "yugioh": ["Yugioh"],
        "lorcana": ["Lorcana"],
        "onepiece": ["One Piece"],
    },
}


def resolve_category_group_filters(
    category_group: str | None,
    subcategory: str | None = None,
) -> tuple[list[str] | None, str | None]:
    """Resolves a (category_group, subcategory) pair into RPC-ready filter args.

    Returns (category_keywords, platform_group_filter) -- exactly one is
    ever non-None (or both None for an unrecognized/absent group), matching
    search_pricecharting_catalog()'s two mutually-exclusive filter
    mechanisms. Shared by admin's search_catalog_rows and the public
    Discover search below so the two can't drift apart.
    """
    if category_group == PRICECHARTING_VIDEO_GAMES_CATEGORY_KEY:
        if subcategory in PRICECHARTING_PLATFORM_GROUPS:
            return None, subcategory
        return None, ANY_PLATFORM_GROUP
    if category_group in PRICECHARTING_PLATFORM_GROUPS:
        # Back-compat: a bare platform key as category_group (the shape
        # used before Category/Subcategory were split into two params)
        # still works, identical to Video Games + that platform.
        return None, category_group
    subgroups = PRICECHARTING_SUBCATEGORY_GROUPS.get(category_group or "")
    if subgroups and subcategory in subgroups:
        return subgroups[subcategory], None
    keywords = PRICECHARTING_CATEGORY_GROUPS.get(category_group or "")
    return (keywords if keywords else None), None


@dataclass(frozen=True)
class CatalogSearchService:
    supabase_url: str | None = None
    service_role_key: str | None = None
    timeout_seconds: float = 5
    client: httpx.Client | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def search(
        self,
        query: str,
        limit: int = 20,
        *,
        category_group: str | None = None,
        subcategory: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        source: str | None = None,
    ) -> CatalogSearchResponse:
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
        #
        # category_group only prunes the PriceCharting side -- KicksDB has
        # no category/platform taxonomy (same limitation as the admin
        # Catalog screen). min_price/max_price apply to whichever source(s)
        # are actually queried. source, when given, skips fetching the
        # other source entirely rather than fetching-then-discarding.
        normalized_source = source if source in ("pricecharting", "kicksdb") else None
        # A category filter is a PriceCharting-taxonomy concept, and KicksDB
        # rows carry no category/platform group at all -- so they can never
        # satisfy one. Fetching them anyway meant a filtered search still
        # returned sneakers: filtering to Yu-Gi-Oh surfaced "Nike Air Max
        # Muscle 95 Yu-Gi-Oh! Joey" alongside real cards. Treat a category
        # filter as implicitly choosing the source it belongs to, unless the
        # caller named a source explicitly.
        if normalized_source is None and (category_group or subcategory):
            normalized_source = (
                "kicksdb"
                if category_group == SNEAKERS_CATEGORY_KEY
                else "pricecharting"
            )
        pc_rows = (
            self._fetch_rows(
                normalized_query, bounded_limit,
                category_group=category_group, subcategory=subcategory,
                min_price=min_price, max_price=max_price,
            )
            if normalized_source != "kicksdb"
            else []
        )
        kicksdb_rows = (
            self._fetch_kicksdb_rows(
                normalized_query, bounded_limit, min_price=min_price, max_price=max_price,
            )
            if normalized_source != "pricecharting"
            else []
        )

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
        # No inline imageUrl enrichment for most categories here: this is
        # the open, free-to-everyone catalog browse/search surface.
        # Publisher-sourced card/product art (Pokemon/Magic/Yu-Gi-Oh/
        # Lorcana/One Piece/LEGO/Funko) is only rendered inline in
        # detail(), reached by tapping into a specific item to confirm and
        # save it to a portfolio -- a bounded, per-item identification use,
        # not an open image database. We DO still resolve the same match
        # here and expose it as externalImageUrl (see
        # _resolve_external_image_url) -- link-only, opened in an
        # external/in-app browser tab by the client, never rendered inline,
        # which is the lower-risk hyperlink pattern rather than hosting the
        # image inside our own app layout.
        # KicksDB (sneakers) images are unaffected; they come from
        # _kicksdb_row_to_result above, not this enrichment chain.
        if any(not result.imageUrl for result in results):
            enabled_image_categories = self._fetch_enabled_image_categories()
            results = [
                self._resolve_external_image_url(result, enabled_image_categories)
                for result in results
            ]
            # Video games are the one exception: rawg_video_game_catalog is
            # our own bulk-imported table (not a live third-party API call
            # or a publisher's own card art), already rendered inline
            # without restriction on detail() -- so it's reasonable to show
            # it directly on the search results row too, rather than only
            # after tapping in. Runs the full matching chain via one
            # request per distinct platform present in this response, not
            # one per row or per exact title -- see
            # _enrich_pricecharting_video_game_images's own comment for
            # why that stays cheap at this table's scale.
            if "videogames" in enabled_image_categories:
                results = self._enrich_pricecharting_video_game_images(results)
        return CatalogSearchResponse(
            query=normalized_query,
            count=len(results),
            results=results,
        )

    def detail(
        self, catalog_id: str, history_limit: int = 30, currency: str | None = None
    ) -> CatalogDetailResponse:
        normalized_id = str(catalog_id or "").strip()
        bounded_history_limit = max(1, min(history_limit, 90))
        # None (not "USD") means "no conversion requested" -- distinct from
        # normalize_display_currency(None), which would default to AUD and
        # force a conversion no caller asked for. Only convert when the
        # caller (the mobile app, from CollectorProfile.preferredCurrency)
        # actually sent a currency and it differs from the source.
        target_currency = (
            normalize_display_currency(currency) if currency else None
        )
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
                result = self._enrich_with_video_games_image(result)
            history_points = [_history_row_to_point(row) for row in history_rows]
            if target_currency:
                result = result.model_copy(
                    update={
                        "pricing": _convert_catalog_pricing(
                            result.pricing, target_currency=target_currency
                        )
                    }
                )
                history_points = [
                    point.model_copy(
                        update={
                            "pricing": _convert_catalog_pricing(
                                point.pricing, target_currency=target_currency
                            )
                        }
                    )
                    for point in history_points
                ]
            marketplace_listings = self._fetch_marketplace_listings(
                catalog_id=normalized_id,
                product_name=str(row.get("product_name") or ""),
                console_name=str(row.get("console_name") or ""),
                currency=currency,
                upc=_clean(row.get("upc")),
            )
            return CatalogDetailResponse(
                result=result,
                history=history_points,
                marketplaceListings=marketplace_listings,
            )

        # Not a pricecharting_catalog row — try kicksdb_catalog before
        # giving up. The two tables have independent id spaces (no
        # collision risk), so this is a safe fallback, not a guess.
        kicksdb_row = self._fetch_kicksdb_catalog_row(normalized_id)
        if kicksdb_row is not None:
            kicksdb_history_rows = self._fetch_kicksdb_history_rows(
                normalized_id, bounded_history_limit
            )
            kicksdb_result = _kicksdb_row_to_result(
                kicksdb_row,
                _normalize_query(str(kicksdb_row.get("title") or "")),
            )
            kicksdb_history_points = [
                _kicksdb_history_row_to_point(row) for row in kicksdb_history_rows
            ]
            if target_currency:
                kicksdb_result = kicksdb_result.model_copy(
                    update={
                        "pricing": _convert_catalog_pricing(
                            kicksdb_result.pricing, target_currency=target_currency
                        )
                    }
                )
                kicksdb_history_points = [
                    point.model_copy(
                        update={
                            "pricing": _convert_catalog_pricing(
                                point.pricing, target_currency=target_currency
                            )
                        }
                    )
                    for point in kicksdb_history_points
                ]
            return CatalogDetailResponse(
                result=kicksdb_result,
                history=kicksdb_history_points,
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

    def _fetch_rows(
        self,
        query: str,
        limit: int,
        *,
        category_group: str | None = None,
        subcategory: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> list[dict[str, Any]]:
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
        category_keywords, platform_group_filter = resolve_category_group_filters(
            category_group, subcategory
        )
        payload = self._request(
            "POST",
            "/rest/v1/rpc/search_pricecharting_catalog",
            json_payload={
                "search_query": query,
                "result_limit": limit,
                "category_keywords": category_keywords,
                "min_price_cents": int(min_price * 100) if min_price is not None else None,
                "max_price_cents": int(max_price * 100) if max_price is not None else None,
                "platform_group_filter": platform_group_filter,
            },
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

    def _fetch_enabled_marketplace_sources(self) -> set[str]:
        # Same fail-open kill-switch pattern as _fetch_enabled_image_
        # categories, against catalog_marketplace_source_flags -- a
        # separate table from catalog_image_source_flags since this gates
        # live marketplace-listing data, not static publisher art.
        all_sources = {"ebay", "pricecharting"}
        try:
            payload = self._request(
                "GET",
                "/rest/v1/catalog_marketplace_source_flags",
                params={"select": "source,enabled"},
            )
        except Exception:
            return all_sources
        if not isinstance(payload, list):
            return all_sources
        disabled = {
            str(row.get("source"))
            for row in payload
            if isinstance(row, dict) and row.get("enabled") is False
        }
        return all_sources - disabled

    def _fetch_marketplace_listings(
        self,
        *,
        catalog_id: str,
        product_name: str,
        console_name: str,
        currency: str | None,
        upc: str | None = None,
    ) -> list[MarketplaceListing]:
        enabled_sources = self._fetch_enabled_marketplace_sources()
        query = " ".join(part for part in (product_name, console_name) if part).strip()
        listings: list[MarketplaceListing] = []
        if "ebay" in enabled_sources and query:
            listings.extend(
                self._fetch_ebay_listings(catalog_id, query=query, currency=currency, upc=upc)
            )
        if "pricecharting" in enabled_sources and query:
            listings.extend(
                self._fetch_pricecharting_listings(catalog_id, query=query, currency=currency)
            )
        return listings

    def _fetch_ebay_listings(
        self, catalog_id: str, *, query: str, currency: str | None, upc: str | None
    ) -> list[MarketplaceListing]:
        marketplace_id = ebay_marketplace_for_currency(currency)
        cached = self._fetch_ebay_listing_cache(catalog_id, marketplace_id)
        if cached is not None:
            return cached
        try:
            # Reuses this service's own injected client (see the class's
            # `client` field) rather than constructing a bare, real
            # httpx.Client() -- critical for tests: every existing test in
            # this file injects a MockTransport-backed client and relies on
            # nothing here ever reaching the real network. Constructing an
            # independent client here bypassed that entirely and made
            # every detail() test attempt a real eBay OAuth call.
            raw_listings = EbayListingService(client=self.client).search_listings(
                query, marketplace_id=marketplace_id, limit=8, upc=upc
            )
        except Exception:
            # eBay being unavailable must never break the rest of the
            # catalog detail response -- same resilience as every other
            # enrichment source in this file.
            raw_listings = []
        self._write_ebay_listing_cache(catalog_id, marketplace_id, raw_listings)
        return [MarketplaceListing(**row) for row in raw_listings]

    def _fetch_pricecharting_listings(
        self, catalog_id: str, *, query: str, currency: str | None
    ) -> list[MarketplaceListing]:
        cached = self._fetch_pricecharting_listing_cache(catalog_id)
        if cached is None:
            try:
                raw_listings = PriceChartingListingService(client=self.client).get_offers(
                    catalog_id, catalog_title=query, limit=8
                )
            except Exception:
                raw_listings = []
            self._write_pricecharting_listing_cache(catalog_id, raw_listings)
            cached = [MarketplaceListing(**row) for row in raw_listings]
        # PriceCharting's Marketplace API is always USD (a single, US-based
        # marketplace, no per-region selection like eBay) -- converted here
        # at read time using the same static-rate FX logic as the main
        # pricing, rather than caching a separate copy per display
        # currency the way eBay's per-marketplace cache does.
        target_currency = normalize_display_currency(currency) if currency else None
        if not target_currency or target_currency == "USD":
            return cached
        rate = _exchange_rate("USD", target_currency)
        return [
            listing.model_copy(
                update={
                    "price": round(listing.price * rate, 2),
                    "currency": target_currency,
                }
            )
            for listing in cached
        ]

    def _fetch_pricecharting_listing_cache(
        self, catalog_id: str
    ) -> list[MarketplaceListing] | None:
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat() + "Z"
        try:
            payload = self._request(
                "GET",
                "/rest/v1/pricecharting_listing_cache",
                params={
                    "select": "listings",
                    "catalog_id": f"eq.{catalog_id}",
                    "fetched_at": f"gt.{cutoff}",
                    "limit": "1",
                },
            )
        except Exception:
            return None
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        listings = row.get("listings") if isinstance(row, dict) else None
        if not isinstance(listings, list):
            return None
        return [MarketplaceListing(**item) for item in listings if isinstance(item, dict)]

    def _write_pricecharting_listing_cache(
        self, catalog_id: str, listings: list[dict[str, Any]]
    ) -> None:
        try:
            self._request(
                "POST",
                "/rest/v1/pricecharting_listing_cache",
                params={"on_conflict": "catalog_id"},
                json_payload={
                    "catalog_id": catalog_id,
                    "listings": listings,
                    "fetched_at": datetime.utcnow().isoformat() + "Z",
                },
                extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
        except Exception:
            # Caching is an optimization, not a correctness requirement --
            # a failed write just means the next view refetches live too.
            return

    def _fetch_ebay_listing_cache(
        self, catalog_id: str, marketplace_id: str
    ) -> list[MarketplaceListing] | None:
        # A row only comes back if it's both present AND fresher than 24h
        # -- older than that is treated identically to "never cached" (a
        # live refetch), same staleness window as the rest of this
        # codebase's tracked-item refresh crons.
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat() + "Z"
        try:
            payload = self._request(
                "GET",
                "/rest/v1/ebay_listing_cache",
                params={
                    "select": "listings",
                    "catalog_id": f"eq.{catalog_id}",
                    "marketplace_id": f"eq.{marketplace_id}",
                    "fetched_at": f"gt.{cutoff}",
                    "limit": "1",
                },
            )
        except Exception:
            return None
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        listings = row.get("listings") if isinstance(row, dict) else None
        if not isinstance(listings, list):
            return None
        return [MarketplaceListing(**item) for item in listings if isinstance(item, dict)]

    def _write_ebay_listing_cache(
        self, catalog_id: str, marketplace_id: str, listings: list[dict[str, Any]]
    ) -> None:
        try:
            self._request(
                "POST",
                "/rest/v1/ebay_listing_cache",
                params={"on_conflict": "catalog_id,marketplace_id"},
                json_payload={
                    "catalog_id": catalog_id,
                    "marketplace_id": marketplace_id,
                    "listings": listings,
                    "fetched_at": datetime.utcnow().isoformat() + "Z",
                },
                extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
        except Exception:
            # Caching is an optimization, not a correctness requirement --
            # a failed write just means the next view refetches live too.
            return

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

    def _enrich_pricecharting_video_game_images(
        self, results: list[CatalogSearchResult]
    ) -> list[CatalogSearchResult]:
        """Fills imageUrl for video-game search results, one request per
        distinct PLATFORM rather than per row or per exact title.

        Originally this batched only the exact-match tier into a single
        composite-OR request -- fast, but a row needing the prefix/loose-
        match/edition-suffix/"the "-prefix fallbacks (the majority of real
        gaps found in later audits) kept its placeholder until the user
        tapped into detail(), where the full chain runs per item. Revised
        after repeated reports traced back to this same row-vs-detail gap:
        instead of asking Supabase for specific titles, this fetches EVERY
        row for each distinct platform actually present in the candidate
        set ONCE, then runs the full exact -> prefix -> loose -> edition-
        suffix-retry -> "the "-prefix-retry chain locally against that
        already-fetched list (see _match_video_game_with_fallbacks) --
        the same matching power as detail(), just amortized across
        however many rows in this response share a platform instead of
        one HTTP round-trip per tier per row.
        rawg_video_game_catalog is small enough for this to stay cheap:
        confirmed live via EXPLAIN ANALYZE, the single largest platform
        (PC, ~10K of the table's ~54K rows) is a ~77ms sequential scan,
        and a real search response typically touches only 1-4 distinct
        platforms among its video-game rows, not one per row.
        """
        candidates: list[tuple[int, str, str]] = []
        for index, result in enumerate(results):
            if result.imageUrl or not result.setName:
                continue
            rawg_platform = _video_game_rawg_platform(result.setName)
            if rawg_platform is None:
                continue
            base_title = _video_game_base_title(result.title)
            if not base_title:
                continue
            normalized_name = _video_game_normalize_name(base_title)
            if not normalized_name:
                continue
            normalized_name = _video_game_resolve_normalized_name(
                normalized_name, rawg_platform
            )
            candidates.append((index, normalized_name, rawg_platform))

        if not candidates:
            return results

        distinct_platforms = sorted({platform for _, _, platform in candidates})
        rows_by_platform: dict[str, list[dict[str, Any]]] = {}
        for platform in distinct_platforms:
            payload = self._request(
                "GET",
                "/rest/v1/rawg_video_game_catalog",
                params={
                    "select": "normalized_name,image_url",
                    "rawg_platform": f"eq.{platform}",
                    "limit": "20000",
                },
            )
            rows_by_platform[platform] = (
                [row for row in payload if isinstance(row, dict)]
                if isinstance(payload, list)
                else []
            )

        # Dedup (name, platform) pairs -- a search page can easily contain
        # the same title on the same platform twice (different
        # conditions/printings), no need to re-run the match twice.
        image_by_pair: dict[tuple[str, str], str] = {}
        for _, name, platform in candidates:
            key = (name, platform)
            if key in image_by_pair:
                continue
            image_url = _match_video_game_with_fallbacks(
                name, rows_by_platform.get(platform, [])
            )
            if image_url:
                image_by_pair[key] = image_url

        updated = list(results)
        for index, name, platform in candidates:
            image_url = image_by_pair.get((name, platform))
            if image_url:
                updated[index] = updated[index].model_copy(
                    update={"imageUrl": image_url}
                )
        return updated

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
        # Images come from rawg_video_game_catalog, our own table bulk-
        # imported from RAWG's API (53,890 real game+platform rows across
        # the same ~24 mainstream platforms in
        # _VIDEO_GAME_PLATFORM_RAWG_MAP -- see scripts/import_rawg_
        # video_game_catalog.py). This used to be a live RAWG lookup +
        # write-through cache, but with the real bulk data local there's
        # no reason left to call RAWG at request time. Same conservative
        # discipline as every other category: normalized_name +
        # rawg_platform must resolve to exactly one row (RAWG can have
        # more than one entry for the same normalized name + platform,
        # e.g. remasters/re-releases) -- zero or ambiguous matches ->
        # suppress, never guess.
        if result.imageUrl or not result.setName:
            return result
        rawg_platform = _video_game_rawg_platform(result.setName)
        if rawg_platform is None:
            return result
        base_title = _video_game_base_title(result.title)
        if not base_title:
            return result

        normalized_name = _video_game_normalize_name(base_title)
        if not normalized_name:
            return result
        normalized_name = _video_game_resolve_normalized_name(normalized_name, rawg_platform)
        image_url = self._fetch_video_game_image(normalized_name, rawg_platform)
        if image_url is None:
            return result
        return result.model_copy(update={"imageUrl": image_url})

    def _fetch_video_game_image(
        self, normalized_name: str, rawg_platform: str
    ) -> str | None:
        image_url = self._fetch_video_game_image_chain(normalized_name, rawg_platform)
        if image_url is not None:
            return image_url
        # Last-resort fallback for a real, live-audited gap: a systematic
        # cross-reference of every real PriceCharting video-game title
        # against rawg_video_game_catalog found ~1,083 "Remastered"/"HD"/
        # "Definitive Edition"/"Complete Edition"/etc. listings with no
        # match through any tier above, and confirmed via RAWG's live
        # search API that most of these games DO exist in RAWG -- just
        # under the base title, without a separate remaster-specific
        # entry (remasters near-universally reuse the base release's own
        # key art, unlike a full remake -- see the exclusion note on
        # _VIDEO_GAME_EDITION_SUFFIX_RE for why "remake" is deliberately
        # NOT in that pattern). Stripping a trailing edition/remaster
        # suffix and re-running the exact/prefix/loose chain against the
        # stripped title recovered 56 of those 1,083 safely (verified
        # against real data, no false positives on manual review) -- the
        # rest remain unresolved because RAWG genuinely has no matching
        # entry at all (an actual data-coverage gap, not a matching bug)
        # or the difference isn't a simple trailing suffix.
        stripped = _video_game_strip_edition_suffix(normalized_name)
        base_for_article_check = stripped if stripped is not None else normalized_name
        if stripped is not None:
            image_url = self._fetch_video_game_image_chain(stripped, rawg_platform)
            if image_url is not None:
                return image_url
        # Second last-resort fallback, same live audit: PriceCharting
        # frequently drops a leading "The" that RAWG's own title keeps
        # (e.g. PriceCharting's "Witcher 3: Wild Hunt" vs RAWG's "The
        # Witcher 3: Wild Hunt", "Elder Scrolls V: Skyrim" vs "The Elder
        # Scrolls V: Skyrim") -- recovered 23 more titles this way,
        # including two major franchises. Only ever PREPENDS "the ", never
        # guesses at removing one, so a title genuinely starting with
        # "The" already went through the tiers above unchanged.
        if base_for_article_check.startswith("the "):
            return None
        return self._fetch_video_game_image_chain(
            f"the {base_for_article_check}", rawg_platform
        )

    def _fetch_video_game_image_chain(
        self, normalized_name: str, rawg_platform: str
    ) -> str | None:
        params = {
            "select": "image_url",
            "normalized_name": f"eq.{normalized_name}",
            "rawg_platform": f"eq.{rawg_platform}",
            "limit": "2",
        }
        payload = self._request("GET", "/rest/v1/rawg_video_game_catalog", params=params)
        if isinstance(payload, list) and len(payload) == 1:
            row = payload[0]
            image_url = row.get("image_url") if isinstance(row, dict) else None
            return str(image_url) if image_url else None
        return self._fetch_video_game_image_by_prefix(normalized_name, rawg_platform)

    def _fetch_video_game_image_by_prefix(
        self, normalized_name: str, rawg_platform: str
    ) -> str | None:
        # Fallback for the far more common gap than a franchise reboot
        # sharing a title (_VIDEO_GAME_TITLE_ALIASES): RAWG's own title is
        # simply longer than PriceCharting's -- a trailing "!"/"DX"/
        # "Remastered"/edition suffix, or a subtitle PriceCharting's
        # listing omits (e.g. "Brothers" -> "Brothers: A Tale of Two
        # Sons"). A leading-prefix match on normalized_name is backed by
        # rawg_video_game_catalog_lookup_idx (a btree index on
        # (normalized_name, rawg_platform), confirmed live: ~0.2-0.6s per
        # lookup) -- a real index-supported prefix scan, not the kind of
        # unindexed filter that has caused real production timeouts on
        # pricecharting_catalog elsewhere in this codebase.
        #
        # Same conservative discipline as everywhere else: a title like
        # "Doom" prefix-matches DOOM (2016), DOOM Eternal, Doom 3, DOOM II,
        # and Doom 3: BFG Edition on PS4 alone -- multiple genuinely
        # different games, not just longer spellings of the same one, so
        # this correctly suppresses rather than guessing when several rows
        # match. Only a title that prefix-matches exactly one row is used.
        #
        # A single unique match ISN'T automatically safe on its own,
        # though -- a live, systematic audit of every real PriceCharting
        # video-game title against rawg_video_game_catalog found 95 real
        # cases where the ONLY prefix match is a numbered sequel, not a
        # longer title for the same game (e.g. PriceCharting's "Terminator"
        # uniquely prefix-matching RAWG's "Terminator 2: Judgment Day",
        # PriceCharting's "Iron Man" uniquely matching "Iron Man 2") --
        # confirmed those originals simply have no OTHER RAWG entry to
        # create ambiguity, so uniqueness alone let a wrong sequel's cover
        # through. _video_game_prefix_suffix_is_safe rejects a match whose
        # suffix (what comes after the matched prefix) looks like a
        # sequel/numbered-installment continuation rather than an edition/
        # subtitle/release-year suffix for the SAME game -- a rejected
        # match falls through to the loose-match tier below, which
        # requires fold-EXACT equality (not just a shared prefix) and so
        # correctly returns nothing rather than guessing.
        params = {
            "select": "normalized_name,image_url",
            "normalized_name": f"like.{_video_game_like_escape(normalized_name)}*",
            "rawg_platform": f"eq.{rawg_platform}",
            "limit": "2",
        }
        payload = self._request("GET", "/rest/v1/rawg_video_game_catalog", params=params)
        if isinstance(payload, list) and len(payload) == 1:
            row = payload[0]
            candidate_name = str(row.get("normalized_name") or "") if isinstance(row, dict) else ""
            if _video_game_prefix_suffix_is_safe(normalized_name, candidate_name):
                image_url = row.get("image_url") if isinstance(row, dict) else None
                return str(image_url) if image_url else None
        return self._fetch_video_game_image_by_loose_match(normalized_name, rawg_platform)

    def _fetch_video_game_image_by_loose_match(
        self, normalized_name: str, rawg_platform: str
    ) -> str | None:
        # Fallback for the gap neither exact nor prefix matching can catch:
        # a punctuation difference in the MIDDLE of the title, not just a
        # trailing suffix. Live-confirmed real case: PriceCharting's
        # "Uncharted 4 A Thief's End" (straight apostrophe, no colon) vs.
        # RAWG's "Uncharted 4: A Thief's End" (curly apostrophe, colon
        # after the number) -- the colon alone breaks a leading-prefix
        # match immediately after "Uncharted 4", regardless of the
        # apostrophe difference.
        #
        # Can't be a SQL-side filter without a new stripped-punctuation
        # column (normalized_name still has the original punctuation) --
        # so this fetches a small, cheap candidate set and does the real
        # comparison locally: both sides stripped to bare alphanumerics
        # before comparing, so "Uncharted 4: A Thief's End" (curly ’) and
        # "Uncharted 4 A Thief's End" (straight ') both reduce to
        # "uncharted 4 a thiefs end" and match. Same uniqueness
        # requirement as every other tier -- a shared filter word among
        # genuinely different games (e.g. "Doom" -> DOOM 2016 / Eternal /
        # 3 / II) will never loosely-equal each other once compared, so
        # this can't accidentally resolve an ambiguous title.
        #
        # The filter word is the LONGEST word in the title (excluding
        # "the"/"a"/"an"), used as an ilike CONTAINS pattern
        # (ilike.*word*), not a leading-prefix pattern -- live-confirmed
        # real gap with a prefix-anchored approach: for a query like "the
        # witcher 3 wild hunt" (reconstructed by _fetch_video_game_image's
        # "the " fallback, see that method), the real RAWG row ALSO starts
        # with "the ", so filtering on "witcher" as a PREFIX pattern
        # (requiring the candidate to literally start with "witcher")
        # never matches "the witcher 3: wild hunt" at all -- the word's
        # position in the string can't be assumed. A contains pattern
        # sidesteps that; rawg_video_game_catalog is small enough (~54K
        # rows total, confirmed live via EXPLAIN ANALYZE: a full ilike
        # contains-scan takes ~135ms) that an unindexed contains scan here
        # is fine -- a fundamentally different scale than the multi-
        # million-row pricecharting_catalog elsewhere in this codebase,
        # where the same pattern would be a real production risk.
        words = normalized_name.split(" ") if normalized_name else []
        filter_words = [w for w in words if w not in ("the", "a", "an")] or words
        if not filter_words:
            return None
        filter_word = max(filter_words, key=len)
        stripped_target = _video_game_strip_punctuation(normalized_name)
        if not stripped_target:
            return None
        params = {
            "select": "normalized_name,image_url",
            "normalized_name": f"ilike.*{_video_game_like_escape(filter_word)}*",
            "rawg_platform": f"eq.{rawg_platform}",
            "limit": "50",
        }
        payload = self._request("GET", "/rest/v1/rawg_video_game_catalog", params=params)
        if not isinstance(payload, list):
            return None
        matches = [
            row
            for row in payload
            if isinstance(row, dict)
            and _video_game_strip_punctuation(str(row.get("normalized_name") or "")) == stripped_target
        ]
        if len(matches) != 1:
            return None
        image_url = matches[0].get("image_url")
        return str(image_url) if image_url else None

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

    def _fetch_kicksdb_rows(
        self,
        query: str,
        limit: int,
        *,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> list[dict[str, Any]]:
        # Mirrors _fetch_rows()'s RPC-based ranking (see that method's
        # comment for why a plain REST ORDER BY isn't safe here either).
        # kicksdb_catalog is ~11K rows, well under the size where the
        # adaptive broad-query fallback used for pricecharting_catalog
        # becomes necessary. No category_group here -- KicksDB has no
        # category/platform taxonomy (see resolve_category_group_filters'
        # own module).
        payload = self._request(
            "POST",
            "/rest/v1/rpc/search_kicksdb_catalog",
            json_payload={
                "search_query": query,
                "result_limit": limit,
                "min_price_cents": int(min_price * 100) if min_price is not None else None,
                "max_price_cents": int(max_price * 100) if max_price is not None else None,
            },
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


def _convert_catalog_pricing(
    pricing: CatalogSearchPricing, *, target_currency: str
) -> CatalogSearchPricing:
    # Reuses the same static-rate FX logic currency_conversion.py already
    # uses for scan pricing (_exchange_rate/settings.fx_usd_to_*) -- one
    # source of truth for exchange rates, not a second one invented here.
    # PriceCharting/KicksDB data is always sourced in USD, so this only
    # ever converts FROM USD, but _exchange_rate itself is general (handles
    # same-currency as a 1.0 no-op).
    source_currency = pricing.currency or "USD"
    normalized_target = normalize_display_currency(target_currency)
    if normalized_target == source_currency:
        return pricing
    rate = _exchange_rate(source_currency, normalized_target)
    return pricing.model_copy(
        update={
            "currency": normalized_target,
            "originalCurrency": source_currency,
            "marketValue": _convert_catalog_amount(pricing.marketValue, rate),
            "lowEstimate": _convert_catalog_amount(pricing.lowEstimate, rate),
            "highEstimate": _convert_catalog_amount(pricing.highEstimate, rate),
            "loosePrice": _convert_catalog_amount(pricing.loosePrice, rate),
            "cibPrice": _convert_catalog_amount(pricing.cibPrice, rate),
            "newPrice": _convert_catalog_amount(pricing.newPrice, rate),
            "gradedPrice": _convert_catalog_amount(pricing.gradedPrice, rate),
        }
    )


def _convert_catalog_amount(value: float | None, rate: float) -> float | None:
    if value is None:
        return None
    return round(value * rate, 2)


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


_VIDEO_GAME_WHITESPACE_RE = re.compile(r"\s+")


def _video_game_normalize_name(name: str) -> str:
    # MUST stay identical to normalize_name() in scripts/import_rawg_
    # video_game_catalog.py -- that's the normalization applied to every
    # row's normalized_name column at import time, and this side of the
    # match has to produce byte-identical output or nothing will ever
    # match.
    return _VIDEO_GAME_WHITESPACE_RE.sub(" ", name).strip().lower()


def _video_game_like_escape(normalized_name: str) -> str:
    # Postgres's default LIKE escape char is backslash -- a literal "%" or
    # "_" in a game title (rare, but not impossible) would otherwise act
    # as a SQL wildcard instead of a literal character once appended with
    # our own trailing "*" (-> "%") for the prefix match. Order matters:
    # backslash itself must be escaped first, or escaping "%"/"_" after
    # would double-escape their own inserted backslashes.
    return normalized_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Matches a suffix that starts with a bare number or roman numeral --
# "2", "3", "ii", "iv", etc. -- immediately after the matched prefix
# (optionally preceded by whitespace, e.g. "terminator" + " 2: judgment
# day"). This is the numbered-sequel/installment pattern, not an edition/
# subtitle/release-year suffix for the same game (those instead start with
# "(", ":", "-", "!", or a letter that isn't itself a roman numeral word,
# e.g. "remaster"/"dx"/"deluxe" -- none of which this pattern matches, so
# they're unaffected). \b anchors "x" so it doesn't also match the start of
# an unrelated word like "xtreme".
_VIDEO_GAME_SEQUEL_SUFFIX_RE = re.compile(
    r"^\s*(\d+|i{1,3}|iv|vi{0,3}|ix|x)\b", re.IGNORECASE
)


def _video_game_prefix_suffix_is_safe(normalized_name: str, candidate_normalized_name: str) -> bool:
    # See _fetch_video_game_image_by_prefix's own comment for the live
    # audit (95 real cases) this fixes generally. `candidate_normalized_
    # name` is only ever passed in here already confirmed to start with
    # `normalized_name` (the SQL prefix filter guarantees that) -- this
    # just checks what comes right after.
    suffix = candidate_normalized_name[len(normalized_name):]
    return _VIDEO_GAME_SEQUEL_SUFFIX_RE.match(suffix) is None


_VIDEO_GAME_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")


def _video_game_fold_diacritics(text: str) -> str:
    # Found while investigating a real mismatch (RAWG's "God of War:
    # Ragnarök" vs PriceCharting's "God of War Ragnarok" -- the ACTUAL bug
    # in that specific case turned out to be a different, prefix-match
    # collision, fixed separately via _VIDEO_GAME_TITLE_ALIASES; see that
    # table's comment). But auditing the fold logic surfaced a second, real
    # bug this fixes generally: without this, the bare non-alphanumeric
    # strip below DELETES an accented letter like "ö" outright (it isn't in
    # [a-z0-9 ]) rather than folding it to its plain "o" -- "ragnarök" ->
    # "ragnark" (one letter short, an actual wrong string), not "ragnarok"
    # (the correct fold). That silently broke the loose-match comparison
    # for every accented title, not just this one -- a live count found
    # 1,357 of 53,890 rawg_video_game_catalog rows (~2.5%) contain a
    # character outside plain ASCII letters/digits/common punctuation, so
    # this needed a general fix, not a one-off alias table entry per
    # affected title.
    #
    # NFKD decomposes an accented Latin letter into its base letter plus a
    # separate combining diacritical mark (e.g. "ö" -> "o" + COMBINING
    # DIAERESIS); dropping every character in Unicode category "Mn"
    # (nonspacing mark) then leaves just the plain base letters. Symbols
    # that aren't letter+diacritic pairs (√, ×, #, fullwidth punctuation,
    # etc.) don't decompose this way and fall through unchanged -- the
    # existing non-alphanumeric strip below still removes those exactly as
    # it did before this function existed, so this only changes behavior
    # for genuinely accented letters.
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def _video_game_strip_punctuation(normalized_name: str) -> str:
    # For the loose-match fallback only (_fetch_video_game_image_by_
    # loose_match) -- reduces a title to bare alphanumerics + single
    # spaces, so colon-vs-no-colon and straight-vs-curly-apostrophe
    # differences between PriceCharting's and RAWG's titles for the same
    # game (e.g. "Uncharted 4 A Thief's End" vs "Uncharted 4: A Thief's
    # End"), and accented-vs-plain letter differences (e.g. "Ragnarök" vs
    # "Ragnarok"), stop mattering. Never used to build the actual SQL
    # filter (normalized_name in the database still has real punctuation
    # and diacritics) -- only for the local equality comparison against
    # fetched candidates.
    #
    # A hyphen is replaced with a space FIRST, before the general strip
    # below deletes it outright -- live-confirmed real gap: RAWG's
    # "E.T. the Extra-Terrestrial" vs PriceCharting's "ET the Extra
    # Terrestrial" (a space, no hyphen) only differ by that one
    # character, but deleting the hyphen with no replacement joins the
    # two words into "extraterrestrial", one word short of PriceCharting's
    # "extra terrestrial" -- a real string, just the wrong one, so they
    # never compared equal even though the games are actually the same.
    folded = _video_game_fold_diacritics(normalized_name).replace("-", " ")
    return _VIDEO_GAME_WHITESPACE_RE.sub(" ", _VIDEO_GAME_NON_ALNUM_RE.sub("", folded)).strip()


# RAWG disambiguates same-named franchise entries with a suffix RAWG itself
# chose (roman numeral, subtitle, or release year) -- e.g. the 2005 PS2
# original is "God of War I" and the 2018 PS4 reboot is "God of War
# (2018)", but PriceCharting's title for both is literally just "God of
# War", with no disambiguator at all. An exact normalized-name match can
# never bridge that gap on its own (nor should it guess -- picking the
# wrong entry would hand the 2005 original's cover to the 2018 reboot's
# listing or vice versa), so this is a small, hand-verified allowlist of
# (product normalized_name, rawg_platform) -> RAWG's own normalized_name,
# each confirmed live against rawg_video_game_catalog before being added.
# Keyed by platform (not guessed some other way) because a console only
# ever had one disc literally titled "God of War" with nothing else on
# it -- PS2 only ever shipped the 2005 original that way, PS4 only ever
# shipped the 2018 reboot that way, so the platform alone disambiguates
# which real game a bare "God of War" listing on that console actually is.
_VIDEO_GAME_TITLE_ALIASES: dict[tuple[str, str], str] = {
    ("god of war", "PlayStation 2"): "god of war i",
    ("god of war", "PlayStation 4"): "god of war (2018)",
    # This one is NOT the accented-letter gap (ö vs o) -- that part is
    # handled generally by _video_game_fold_diacritics(), no alias needed.
    # The actual bug live-confirmed here is upstream of that: the prefix
    # fallback tier (_fetch_video_game_image_by_prefix) does a raw,
    # unfolded `normalized_name ilike 'god of war ragnarok%'` check, and
    # "god of war ragnarok: valhalla" -- a SEPARATE, real DLC entry, not a
    # longer title for the same base game -- happens to satisfy that raw
    # prefix uniquely, so the prefix tier returned it (and never got as
    # far as the correctly-folding loose-match tier at all). That's a
    # structurally different failure than a punctuation/accent gap: a
    # short title can be a genuine raw-byte prefix of an unrelated
    # product's title, and no amount of string-normalization fixes that --
    # it needs to be told apart by hand, the same reasoning as the two
    # entries above.
    ("god of war ragnarok", "PlayStation 4"): "god of war: ragnarök",
    ("god of war ragnarok", "PlayStation 5"): "god of war: ragnarök",
}


def _video_game_resolve_normalized_name(normalized_name: str, rawg_platform: str) -> str:
    return _VIDEO_GAME_TITLE_ALIASES.get((normalized_name, rawg_platform), normalized_name)


# Trailing remaster/edition suffix, stripped as a last-resort fallback in
# _fetch_video_game_image -- see that method's comment for the live audit
# (1,083 candidates, 56 safely recovered) behind this. Deliberately
# EXCLUDES "remake": a remake (Final Fantasy VII Remake, Resident Evil 4
# Remake) is a distinct, separately-developed product with its own real
# box art, not a technical re-release reusing the original's key art the
# way a remaster/"HD"/"definitive edition"/"complete edition" almost
# always does -- live-confirmed stripping "remake" would have shown the
# ORIGINAL 1997 Final Fantasy VII's cover on a "Final Fantasy VII Remake"
# listing, the same class of wrong-product bug as the sequel-mismatch
# fix above, just introduced by this fallback instead of the prefix tier.
# Anchored at the end ($) so a game genuinely titled e.g. "Definitive
# Edition Simulator" is never touched -- only a real trailing suffix.
_VIDEO_GAME_EDITION_SUFFIX_RE = re.compile(
    r"[:\-]?\s*(remastered|remaster|hd|definitive edition|complete edition|"
    r"goty edition|game of the year edition|game of the year|anniversary edition|"
    r"anniversary|enhanced edition|deluxe edition|special edition)\s*$",
    re.IGNORECASE,
)


def _video_game_strip_edition_suffix(normalized_name: str) -> str | None:
    """Strips a trailing remaster/edition suffix, if present.

    Returns None (not the unchanged string) when nothing was stripped, so
    the caller can tell "no suffix found, don't bother retrying" apart
    from "stripped down to an empty string" -- both are real, distinct
    outcomes it needs to handle differently.
    """
    match = _VIDEO_GAME_EDITION_SUFFIX_RE.search(normalized_name)
    if not match:
        return None
    stripped = normalized_name[: match.start()].strip()
    return stripped or None


def _match_video_game_normalized_name(
    normalized_name: str, platform_rows: list[dict[str, Any]]
) -> str | None:
    """Runs the exact -> prefix -> loose-match chain against an already-
    fetched, complete per-platform row list, instead of one SQL round-trip
    per tier per row -- see _enrich_pricecharting_video_game_images for
    why this exists (the batched search-row path). Same three-tier
    priority and the same uniqueness/safety discipline as the single-item
    live-query chain (_fetch_video_game_image_chain and friends below):
    an ambiguous or unsafe match is suppressed, never guessed.
    """
    if not normalized_name:
        return None

    exact = [
        row
        for row in platform_rows
        if row.get("normalized_name") == normalized_name
    ]
    if len(exact) == 1:
        image_url = exact[0].get("image_url")
        return str(image_url) if image_url else None

    prefix_matches = [
        row
        for row in platform_rows
        if isinstance(row.get("normalized_name"), str)
        and row["normalized_name"].startswith(normalized_name)
    ]
    if len(prefix_matches) == 1:
        candidate_name = str(prefix_matches[0].get("normalized_name") or "")
        if _video_game_prefix_suffix_is_safe(normalized_name, candidate_name):
            image_url = prefix_matches[0].get("image_url")
            return str(image_url) if image_url else None

    stripped_target = _video_game_strip_punctuation(normalized_name)
    if stripped_target:
        loose_matches = [
            row
            for row in platform_rows
            if _video_game_strip_punctuation(str(row.get("normalized_name") or ""))
            == stripped_target
        ]
        if len(loose_matches) == 1:
            image_url = loose_matches[0].get("image_url")
            return str(image_url) if image_url else None

    return None


def _match_video_game_with_fallbacks(
    normalized_name: str, platform_rows: list[dict[str, Any]]
) -> str | None:
    """Adds the edition-suffix and "the "-prefix retries on top of
    _match_video_game_normalized_name -- the local-matching counterpart of
    _fetch_video_game_image's own two last-resort fallbacks, see that
    method's comments for why each exists.
    """
    image_url = _match_video_game_normalized_name(normalized_name, platform_rows)
    if image_url is not None:
        return image_url

    stripped = _video_game_strip_edition_suffix(normalized_name)
    base_for_article_check = stripped if stripped is not None else normalized_name
    if stripped is not None:
        image_url = _match_video_game_normalized_name(stripped, platform_rows)
        if image_url is not None:
            return image_url

    if base_for_article_check.startswith("the "):
        return None
    return _match_video_game_normalized_name(
        f"the {base_for_article_check}", platform_rows
    )


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
