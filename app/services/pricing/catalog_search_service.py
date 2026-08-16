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
    # TCGdex (api.tcgdex.net) is a separate free, public, no-key-required
    # API -- not Supabase -- so it gets its own client/timeout. A short
    # timeout matters here specifically: this is a live external call made
    # during search(), and a TCGdex outage must not stall our own response.
    tcgdex_client: httpx.Client | None = None
    tcgdex_timeout_seconds: float = 3

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
        results = [self._enrich_with_funko_image(result) for result in results]
        results = [self._enrich_with_pokemon_image(result) for result in results]
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
            return CatalogDetailResponse(
                result=self._enrich_with_pokemon_image(
                    self._enrich_with_funko_image(result)
                ),
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
        # PriceCharting has no image data for Pokemon cards either. Real
        # tested coverage against our own vintage/international-heavy
        # catalog is low overall (~14% on a real sample — most rows are
        # Japanese/Korean prints or pre-2021 sets no free source covers),
        # so this is deliberately narrow: only the handful of classic
        # English sets in _POKEMON_SET_TCGDEX_IDS, each verified live
        # against TCGdex (api.tcgdex.net, free/no key) before being added.
        # No set-name mapping means no attempt — never a fuzzy guess.
        if result.imageUrl or not result.setName or "pokemon" not in result.setName.lower():
            return result
        set_id = _POKEMON_SET_TCGDEX_IDS.get(result.setName.strip().lower())
        if set_id is None:
            return result
        card_number = _pokemon_card_number(result.title)
        if card_number is None:
            return result
        image_url = self._fetch_pokemon_image(set_id, card_number)
        if image_url is None:
            return result
        return result.model_copy(update={"imageUrl": image_url})

    def _fetch_pokemon_image(self, set_id: str, card_number: str) -> str | None:
        client = self.tcgdex_client or httpx.Client(timeout=self.tcgdex_timeout_seconds)
        should_close = self.tcgdex_client is None
        try:
            response = client.get(
                f"https://api.tcgdex.net/v2/en/cards/{set_id}-{card_number}"
            )
            if response.status_code != 200:
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            # A TCGdex hiccup means no image this time, not a broken
            # search — same fail-open behavior as a missing Funko match.
            return None
        finally:
            if should_close:
                client.close()
        if not isinstance(payload, dict):
            return None
        image_base = payload.get("image")
        if not image_base or not isinstance(image_base, str):
            return None
        return f"{image_base}/high.png"

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
    ) -> Any:
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
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


# Verified live against TCGdex (api.tcgdex.net/v2/en/cards/{id}-{number})
# before being added -- each key confirmed to return the correct card name,
# correct set, and a real image. Keys are PriceCharting's exact console_name
# values, lowercased. Deliberately small and hand-maintained: real tested
# match rate against our catalog is low overall (~14% on a random sample,
# mostly because our Pokemon rows skew Japanese/Korean/vintage, which no
# free image source covers), so this only covers a few classic English sets
# rather than guessing at a broader mapping.
_POKEMON_SET_TCGDEX_IDS: dict[str, str] = {
    "pokemon base set": "base1",
    "pokemon jungle": "base2",
    "pokemon fossil": "base3",
    "pokemon base set 2": "base4",
    "pokemon team rocket": "base5",
}
_POKEMON_CARD_NUMBER_RE = re.compile(r"#(\w+)")


def _pokemon_card_number(product_title: str) -> str | None:
    match = _POKEMON_CARD_NUMBER_RE.search(product_title)
    return match.group(1) if match else None


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
