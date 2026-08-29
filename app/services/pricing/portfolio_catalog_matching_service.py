from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.pricing.catalog_search_service import (
    CatalogSearchError,
    CatalogSearchService,
)
from app.services.pricing.currency_conversion import _exchange_rate


class PortfolioCatalogMatchingError(Exception):
    """Raised when portfolio-to-catalog matching cannot be completed."""


# CatalogSearchService buckets its raw match score into a coarse confidence:
# score>=100 (exact) -> 0.96, score>=80 (prefix or product-name substring)
# -> 0.90, score>=55 (console/category substring) -> 0.78, else -> 0.62.
# Auto-linking on a console/category-only match (0.78) risks pointing a
# portfolio item at a plausible-sounding but wrong product, silently showing
# a wrong price -- worse than the live-API fallback this replaces. Requiring
# >=0.90 keeps only exact/prefix/product-name-substring matches.
MIN_AUTO_LINK_CONFIDENCE = 0.90


@dataclass(frozen=True)
class MatchResult:
    candidateCount: int
    matchedCount: int
    unmatchedCount: int
    skippedMissingTitle: int
    updateFailures: int = 0
    historyRowsBackfilled: int = 0


def match_unlinked_portfolio_items(
    *,
    limit: int = 200,
    dry_run: bool = True,
    timeout_seconds: float = 30,
) -> MatchResult:
    reader = PortfolioItemReader(
        supabase_url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key,
        timeout_seconds=timeout_seconds,
    )
    catalog = CatalogSearchService(
        supabase_url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key,
        timeout_seconds=timeout_seconds,
    )

    items = reader.fetch_unlinked_items(limit=limit)
    matched = 0
    unmatched = 0
    skipped_missing_title = 0
    updates: list[dict[str, Any]] = []
    history_rows_backfilled = 0
    now = _utc_now_iso()

    for item in items:
        query = build_match_query(item)
        if not query:
            skipped_missing_title += 1
            continue
        match = find_best_match(catalog, query)
        if match is not None:
            matched += 1
            updates.append(
                {
                    "id": item["id"],
                    "user_id": item["user_id"],
                    "pricecharting_id": match["id"],
                    "pricecharting_match_score": match["score"],
                    "pricecharting_matched_at": now,
                    "pricecharting_match_attempted_at": now,
                }
            )
            if not dry_run:
                history_rows_backfilled += backfill_catalog_history_for_item(
                    catalog,
                    reader,
                    item=item,
                    catalog_id=match["id"],
                    match_source=match["source"],
                    match_confidence=match["confidence"],
                )
        else:
            unmatched += 1
            updates.append(
                {
                    "id": item["id"],
                    "user_id": item["user_id"],
                    "pricecharting_match_attempted_at": now,
                }
            )

    update_failures = 0
    if not dry_run and updates:
        update_failures = reader.apply_updates(updates)

    return MatchResult(
        candidateCount=len(items),
        matchedCount=matched,
        unmatchedCount=unmatched,
        skippedMissingTitle=skipped_missing_title,
        updateFailures=update_failures,
        historyRowsBackfilled=history_rows_backfilled,
    )


def build_match_query(item: dict[str, Any]) -> str:
    # Mirrors what a user would type manually searching Discover for the
    # same item -- title alone is usually specific enough (AI-generated
    # titles tend to read like "Amazing Spider-Man #300", not just "comic").
    title = str(item.get("title") or "").strip()
    return title


def find_best_match(catalog: CatalogSearchService, query: str) -> dict[str, Any] | None:
    try:
        response = catalog.search(query, limit=5)
    except CatalogSearchError:
        return None
    for result in response.results:
        if result.confidence >= MIN_AUTO_LINK_CONFIDENCE:
            # pricecharting_match_score is an integer column -- store as a
            # 0-100 percentage (matches RepricePricingResponse.pricingConfidence's
            # existing int convention elsewhere in this codebase), not the raw
            # 0.0-1.0 float confidence (writing that raw crashed with Postgres
            # 22P02 "invalid input syntax for type integer" on a live run).
            return {
                "id": result.id,
                "score": round(result.confidence * 100),
                "source": result.source,
                "confidence": result.confidence,
            }
    return None


def backfill_catalog_history_for_item(
    catalog: CatalogSearchService,
    reader: "PortfolioItemReader",
    *,
    item: dict[str, Any],
    catalog_id: str,
    match_source: str,
    match_confidence: float,
) -> int:
    """Backfills real historical price points from pricecharting_catalog_history
    for a newly-matched item, for dates before its earliest existing real
    snapshot (if any). Not fabricated data: every point is a real SCD2 price
    version PriceCharting recorded for the SAME matched product -- this just
    makes it visible on the item's own chart instead of only starting from
    whenever it was actually scanned. Runs once, right when a match is first
    found (matched items never re-enter the unlinked queue), so no dedup
    bookkeeping is needed beyond the earliest-snapshot cutoff itself."""
    try:
        detail = catalog.detail(catalog_id, history_limit=90)
    except CatalogSearchError:
        return 0

    earliest_existing = reader.fetch_earliest_snapshot_at(
        item_id=item["id"], user_id=item["user_id"]
    )
    display_currency = _display_currency_from_item(item)

    rows: list[dict[str, Any]] = []
    for point in detail.history:
        if earliest_existing is not None and point.validFrom >= earliest_existing:
            continue
        value = point.pricing.marketValue
        if value is None or value <= 0:
            continue
        rate = _exchange_rate(point.pricing.currency, display_currency)
        low = point.pricing.lowEstimate if point.pricing.lowEstimate else value
        high = point.pricing.highEstimate if point.pricing.highEstimate else value
        rows.append(
            {
                "user_id": item["user_id"],
                "portfolio_item_id": item["id"],
                "value_aud": value * rate,
                "low_estimate_aud": low * rate,
                "high_estimate_aud": high * rate,
                "display_string": None,
                "valuation_status": "market_estimated",
                "reason_code": "catalog_history_backfill",
                "valuation_strategy": "catalog_lookup",
                "pricing_provider": match_source,
                "attribution_text": match_source,
                "confidence_score": match_confidence,
                "condition_label": None,
                "priced_at": point.validFrom,
                "evidence_json": {
                    "source": "catalog_history_backfill",
                    "pricechartingId": catalog_id,
                },
            }
        )

    return reader.insert_snapshots(rows)


def _display_currency_from_item(item: dict[str, Any]) -> str:
    raw = item.get("raw_json") or {}
    currency = str(raw.get("currency") or "").strip()
    if currency:
        return currency
    pricing_currency = str((raw.get("pricing") or {}).get("currency") or "").strip()
    return pricing_currency or "AUD"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PortfolioItemReader:
    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        timeout_seconds: float = 30,
        client: httpx.Client | None = None,
    ) -> None:
        self._supabase_url = supabase_url.strip().rstrip("/")
        self._service_role_key = service_role_key.strip()
        self._timeout_seconds = timeout_seconds
        self._client = client
        if not self._supabase_url or not self._service_role_key:
            raise PortfolioCatalogMatchingError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required."
            )

    def fetch_unlinked_items(self, *, limit: int) -> list[dict[str, Any]]:
        params = {
            "select": "id,user_id,title,category,raw_json",
            "pricecharting_id": "is.null",
            # Don't spend catalog-match attempts on soft-deleted rows.
            "sync_status": "neq.deleted",
            "order": "pricecharting_match_attempted_at.asc.nullsfirst",
            "limit": str(limit),
        }
        payload = self._request("GET", "/rest/v1/portfolio_items", params=params)
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def apply_updates(self, updates: list[dict[str, Any]]) -> int:
        # PATCH per row (not a batched upsert): portfolio_items' primary key
        # is composite (id, user_id), and each row needs its own values
        # (different pricecharting_id/score/timestamps), which a single
        # shared-payload PATCH can't express. Same reasoning as the set
        # registry's per-row PATCH in backfill_pricecharting_sets.py.
        #
        # A single bad row must not abort every other row's update in this
        # batch (hit for real: one row's stale float score 400'd and the
        # remaining ~19 updates in that run never happened). Log and keep
        # going instead of raising -- the failed row's pricecharting_id
        # stays null and pricecharting_match_attempted_at stays unset, so
        # it's naturally retried next cycle rather than silently stuck.
        failures = 0
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            for update in updates:
                item_id = update["id"]
                user_id = update["user_id"]
                body = {
                    key: value
                    for key, value in update.items()
                    if key not in ("id", "user_id")
                }
                response = client.patch(
                    f"{self._supabase_url}/rest/v1/portfolio_items",
                    params={"id": f"eq.{item_id}", "user_id": f"eq.{user_id}"},
                    headers={**self._headers(), "Prefer": "return=minimal"},
                    json=body,
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    failures += 1
                    print(
                        f"Portfolio item update failed for {item_id}, will retry "
                        f"next cycle: HTTP {response.status_code}: {response.text}"
                    )
        finally:
            if should_close:
                client.close()
        return failures

    def fetch_earliest_snapshot_at(self, *, item_id: str, user_id: str) -> str | None:
        params = {
            "select": "priced_at",
            "portfolio_item_id": f"eq.{item_id}",
            "user_id": f"eq.{user_id}",
            "order": "priced_at.asc",
            "limit": "1",
        }
        payload = self._request(
            "GET", "/rest/v1/portfolio_valuation_snapshots", params=params
        )
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        return row.get("priced_at") if isinstance(row, dict) else None

    def insert_snapshots(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.post(
                f"{self._supabase_url}/rest/v1/portfolio_valuation_snapshots",
                headers={**self._headers(), "Prefer": "return=minimal"},
                json=rows,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                # A missed history backfill is a nice-to-have, not something
                # that should block the match itself from being recorded --
                # log and move on, same posture as _persist_valuation_snapshot
                # in batch_repricing_service.py.
                print(
                    f"Catalog history backfill insert failed for "
                    f"{rows[0].get('portfolio_item_id')}: HTTP "
                    f"{response.status_code}: {response.text}"
                )
                return 0
        finally:
            if should_close:
                client.close()
        return len(rows)

    def _request(self, method: str, path: str, *, params: dict[str, str]) -> Any:
        headers = self._headers()
        headers["Accept"] = "application/json"
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.request(
                method,
                f"{self._supabase_url}{path}",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise PortfolioCatalogMatchingError("Portfolio item request failed.") from error
        finally:
            if should_close:
                client.close()

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Content-Type": "application/json",
        }
