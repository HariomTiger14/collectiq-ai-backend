from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.portfolio import PortfolioItem
from app.schemas.pricing import RepriceIdentityRequest, RepriceRequest, RepriceResponse
from app.services.portfolio_service import portfolio_service
from app.services.pricing.reprice_service import RepriceService


LOW_CONFIDENCE_THRESHOLD = 70
STALE_PRICE_DAYS = 30


class ReviewQueueItemNotFoundError(Exception):
    """Raised when an admin pricing review action targets an unknown item."""


class ReviewQueueItemNotPriceableError(Exception):
    """Raised when a portfolio item lacks the minimum fields needed to retry pricing."""


class ReviewQueueRepositoryError(Exception):
    """Raised when the persistent review queue cannot be read or written."""


class AdminPricingReviewQueueService:
    def __init__(
        self,
        *,
        repository: "SupabasePricingReviewQueueRepository | None" = None,
    ) -> None:
        self._repository = repository or SupabasePricingReviewQueueRepository()

    def list_queue(self, *, reason: str = "all", limit: int = 50) -> dict[str, Any]:
        items = (
            self._repository.list_items(limit=max(limit, 200))
            if self._repository.is_configured
            else portfolio_service.list_items()
        )
        queue_items = [
            review_item
            for item in items
            if (review_item := _review_item_from_portfolio(item)) is not None
        ]
        if reason != "all":
            queue_items = [
                item for item in queue_items if reason in item["reasons"]
            ]

        queue_items.sort(key=_sort_key)
        limited_items = queue_items[:limit]
        return {
            "success": True,
            "filter": reason,
            "count": len(limited_items),
            "totalCount": len(queue_items),
            "thresholds": {
                "lowConfidence": LOW_CONFIDENCE_THRESHOLD,
                "stalePriceDays": STALE_PRICE_DAYS,
            },
            "items": limited_items,
        }

    def mark_reviewed(self, item_id: str) -> dict[str, Any]:
        reviewed_at = _utc_now()
        update_data = {
            "needsReview": False,
            "requiresReview": False,
            "reviewedAt": reviewed_at,
            "reviewStatus": "reviewed",
        }
        if self._repository.is_configured:
            item = self._repository.update_item_data(item_id, update_data)
        else:
            item = portfolio_service.update_item_data(item_id, update_data)
        if item is None:
            raise ReviewQueueItemNotFoundError(f"Portfolio item {item_id} was not found.")
        return {
            "success": True,
            "itemId": item_id,
            "reviewStatus": "reviewed",
        }

    def retry_pricing(self, item_id: str) -> dict[str, Any]:
        item = (
            self._repository.get_item(item_id)
            if self._repository.is_configured
            else portfolio_service.get_item(item_id)
        )
        if item is None:
            raise ReviewQueueItemNotFoundError(f"Portfolio item {item_id} was not found.")
        request = _reprice_request_from_item(item)
        response = RepriceService().reprice(request)
        pricing = response.pricing.model_dump(mode="json")
        display_value = _price_value({}, pricing)
        valuation_status = _valuation_status(pricing)
        update_data = {
            "pricing": pricing,
            "estimatedValue": display_value,
            "valuationStatus": valuation_status,
            "needsReview": response.pricing.pricingConfidence < LOW_CONFIDENCE_THRESHOLD
            or response.pricing.status != "available",
            "lastPricingRetryAt": _utc_now(),
            "reviewStatus": "pricing_retried",
        }
        if self._repository.is_configured:
            self._repository.update_item_data(item_id, update_data)
        else:
            portfolio_service.update_item_data(item_id, update_data)
        return {
            "success": True,
            "itemId": item_id,
            "pricing": pricing,
        }

    def override_price(
        self,
        item_id: str,
        *,
        estimated_value: float,
        currency: str = "USD",
        note: str | None = None,
    ) -> dict[str, Any]:
        item = (
            self._repository.get_item(item_id)
            if self._repository.is_configured
            else portfolio_service.get_item(item_id)
        )
        if item is None:
            raise ReviewQueueItemNotFoundError(f"Portfolio item {item_id} was not found.")
        reviewed_at = _utc_now()
        pricing = {
            **_pricing_payload(item.data),
            "status": "available",
            "estimatedMarketValue": round(float(estimated_value), 2),
            "currency": currency.strip().upper() or "USD",
            "pricingConfidence": 100,
            "confidenceScore": 1.0,
            "pricingSource": {"name": "admin_override"},
            "lastUpdated": reviewed_at,
            "override": True,
        }
        update_data = {
            "pricing": pricing,
            "estimatedValue": pricing["estimatedMarketValue"],
            "valuationStatus": "market_estimated",
            "needsReview": False,
            "requiresReview": False,
            "reviewedAt": reviewed_at,
            "reviewStatus": "admin_override",
            "adminReviewNote": note.strip() if isinstance(note, str) and note.strip() else None,
        }
        if self._repository.is_configured:
            self._repository.update_item_data(item_id, update_data)
        else:
            portfolio_service.update_item_data(item_id, update_data)
        return {
            "success": True,
            "itemId": item_id,
            "reviewStatus": "admin_override",
            "pricing": pricing,
            "note": update_data["adminReviewNote"],
        }

    def assign_item(
        self,
        item_id: str,
        *,
        assignee: str | None = None,
        status: str = "open",
    ) -> dict[str, Any]:
        item = (
            self._repository.get_item(item_id)
            if self._repository.is_configured
            else portfolio_service.get_item(item_id)
        )
        if item is None:
            raise ReviewQueueItemNotFoundError(f"Portfolio item {item_id} was not found.")
        normalized_status = status.strip().lower() if isinstance(status, str) else "open"
        if normalized_status not in {"open", "in_review", "done"}:
            normalized_status = "open"
        normalized_assignee = assignee.strip() if isinstance(assignee, str) and assignee.strip() else None
        updated_at = _utc_now()
        assignment = {
            "assignee": normalized_assignee,
            "status": normalized_status,
            "updatedAt": updated_at,
        }
        update_data = {
            "pricingAssignment": assignment,
            "pricingAssignee": normalized_assignee,
            "pricingAssignmentStatus": normalized_status,
            "pricingAssignmentUpdatedAt": updated_at,
        }
        if self._repository.is_configured:
            self._repository.update_item_data(item_id, update_data)
        else:
            portfolio_service.update_item_data(item_id, update_data)
        return {
            "success": True,
            "itemId": item_id,
            "assignment": assignment,
        }

    def bulk_mark_reviewed(self, item_ids: list[str]) -> dict[str, Any]:
        return _bulk_result(item_ids, self.mark_reviewed)

    def bulk_retry_pricing(self, item_ids: list[str]) -> dict[str, Any]:
        return _bulk_result(item_ids, self.retry_pricing)


class SupabasePricingReviewQueueRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        table_name: str = "portfolio_items",
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
        self._table_name = table_name.strip() or "portfolio_items"
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def list_items(self, *, limit: int = 200, user_id: str | None = None, offset: int = 0) -> list[PortfolioItem]:
        params = {
            "select": "*",
            "limit": str(limit),
            "offset": str(offset),
            "order": "updated_at.desc.nullslast,created_at.desc.nullslast",
        }
        if user_id:
            params["user_id"] = f"eq.{user_id}"
        payload = self._request("GET", f"/rest/v1/{self._table_name}", params=params)
        if not isinstance(payload, list):
            raise ReviewQueueRepositoryError("Supabase portfolio response shape was invalid.")
        return [
            item
            for row in payload
            if isinstance(row, dict) and (item := _portfolio_item_from_row(row)) is not None
        ]

    def batch_owner_display_names(self, user_ids: list[str]) -> dict[str, str]:
        # One request across every distinct owner on the page, not one per
        # item — the same batching approach used for the admin users list.
        # collector_profiles has no email column (email lives only in
        # Supabase Auth, with no efficient bulk-by-id lookup), so this uses
        # display_name as the human-readable "Owner" value instead.
        ids = [uid for uid in dict.fromkeys(user_ids) if uid]
        if not ids:
            return {}
        try:
            payload = self._request(
                "GET",
                "/rest/v1/collector_profiles",
                params={"user_id": "in.(" + ",".join(ids) + ")", "select": "user_id,display_name"},
            )
        except ReviewQueueRepositoryError:
            return {}
        if not isinstance(payload, list):
            return {}
        return {
            str(row["user_id"]): str(row["display_name"])
            for row in payload
            if isinstance(row, dict) and row.get("user_id") and row.get("display_name")
        }

    # Supabase Auth (GoTrue) has no bulk-by-ids admin lookup — only a single-
    # user GET. Callers should only use this for owners that batch_owner_
    # display_names() didn't already cover, and should cap how many distinct
    # owners they're willing to look up this way per page.
    def get_user_email(self, user_id: str) -> str | None:
        try:
            payload = self._request("GET", f"/auth/v1/admin/users/{user_id}")
        except ReviewQueueRepositoryError:
            return None
        if not isinstance(payload, dict):
            return None
        email = payload.get("email")
        return str(email) if email else None

    def get_item(self, item_id: str) -> PortfolioItem | None:
        payload = self._request(
            "GET",
            f"/rest/v1/{self._table_name}",
            params={
                "id": f"eq.{item_id}",
                "select": "*",
                "limit": "1",
            },
        )
        if not isinstance(payload, list):
            raise ReviewQueueRepositoryError("Supabase portfolio response shape was invalid.")
        if not payload:
            return None
        row = payload[0]
        return _portfolio_item_from_row(row) if isinstance(row, dict) else None

    def list_valuation_history_for_item(self, item_id: str) -> list[dict[str, Any]]:
        # Every snapshot ever priced for this item lives here forever --
        # nothing prunes this table -- so this pages through all of it
        # instead of an arbitrary recent-N cap, for a real full-history
        # chart (1M/6M/MAX ranges only mean something if the data behind
        # them isn't already truncated). Paged in chunks of 500 (under
        # PostgREST's default max-rows) with a 20-page safety backstop
        # (10,000 snapshots) against a runaway loop on unexpected data.
        page_size = 500
        max_pages = 20
        rows: list[dict[str, Any]] = []
        for page in range(max_pages):
            try:
                payload = self._request(
                    "GET",
                    "/rest/v1/portfolio_valuation_snapshots",
                    params={
                        "portfolio_item_id": f"eq.{item_id}",
                        "select": "*",
                        "limit": str(page_size),
                        "offset": str(page * page_size),
                        "order": "priced_at.desc",
                    },
                )
            except ReviewQueueRepositoryError:
                break
            if not isinstance(payload, list):
                break
            rows.extend(row for row in payload if isinstance(row, dict))
            if len(payload) < page_size:
                break
        return rows

    def update_item_data(self, item_id: str, data: dict[str, Any]) -> PortfolioItem | None:
        current = self.get_item(item_id)
        if current is None:
            return None
        merged_data = {**current.data, **data}
        payload = self._request(
            "PATCH",
            f"/rest/v1/{self._table_name}",
            params={"id": f"eq.{item_id}", "select": "*"},
            json_payload=_row_update_from_data(merged_data),
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(payload, list) and payload:
            row = payload[0]
            return _portfolio_item_from_row(row) if isinstance(row, dict) else current
        return PortfolioItem(id=item_id, data=merged_data)

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
            raise ReviewQueueRepositoryError("Supabase review queue request failed.") from error
        finally:
            if should_close:
                client.close()


def _review_item_from_portfolio(item: PortfolioItem) -> dict[str, Any] | None:
    data = item.data
    pricing = _pricing_payload(data)
    confidence = _confidence(data, pricing)
    value = _price_value(data, pricing)
    last_priced_at = _first_text(
        data,
        pricing,
        "lastPricedAt",
        "lastUpdated",
        "updatedAt",
        "createdAt",
    )
    explicit_needs_review = bool(data.get("needsReview") or data.get("requiresReview"))

    reasons: list[str] = []
    if explicit_needs_review:
        reasons.append("needs_review")
    if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
        reasons.append("low_confidence")
    if value is None or value <= 0:
        reasons.append("missing_price")
    if _is_stale(last_priced_at):
        reasons.append("stale_price")

    if not reasons:
        return None

    return {
        "id": item.id,
        "title": _first_text(data, "title", "itemName", "name") or "Untitled collectible",
        "category": _first_text(data, "category", "type") or "Unknown",
        "condition": _first_text(data, "condition") or "Unknown",
        "provider": _provider_name(pricing),
        "price": value,
        "currency": _first_text(data, pricing, "currency", "displayCurrency") or "USD",
        "confidence": confidence,
        "reasons": reasons,
        "reasonLabel": _reason_label(reasons),
        "lastPricedAt": last_priced_at,
        "createdAt": _first_text(data, "createdAt", "created_at"),
        "updatedAt": _first_text(data, "updatedAt", "updated_at"),
        "assignment": _assignment_payload(data),
    }


def _assignment_payload(data: dict[str, Any]) -> dict[str, Any]:
    assignment = data.get("pricingAssignment")
    if isinstance(assignment, dict):
        return {
            "assignee": _first_text(assignment, "assignee"),
            "status": _first_text(assignment, "status") or "open",
            "updatedAt": _first_text(assignment, "updatedAt", "updated_at"),
        }
    return {
        "assignee": _first_text(data, "pricingAssignee"),
        "status": _first_text(data, "pricingAssignmentStatus") or "open",
        "updatedAt": _first_text(data, "pricingAssignmentUpdatedAt"),
    }


def _reprice_request_from_item(item: PortfolioItem) -> RepriceRequest:
    data = item.data
    title = _first_text(data, "title", "itemName", "name")
    category = _first_text(data, "category", "type")
    if not title or not category:
        raise ReviewQueueItemNotPriceableError(
            "Title and category are required before retrying pricing."
        )
    return RepriceRequest(
        itemId=item.id,
        previousValue=_price_value(data, _pricing_payload(data)),
        previousCurrency=_first_text(data, "currency", "displayCurrency"),
        displayCurrency=_first_text(data, "displayCurrency", "currency"),
        correctionSource="admin_review_queue",
        identity=RepriceIdentityRequest(
            title=title,
            category=category,
            brand=_first_text(data, "brand"),
            setName=_first_text(data, "setName", "set"),
            series=_first_text(data, "series"),
            cardNumber=_first_text(data, "cardNumber", "number"),
            sku=_first_text(data, "sku"),
            upc=_first_text(data, "upc"),
            condition=_first_text(data, "condition"),
            year=_first_text(data, "year"),
            edition=_first_text(data, "edition"),
            language=_first_text(data, "language"),
            rarity=_first_text(data, "rarity"),
            playerOrCharacter=_first_text(data, "playerOrCharacter"),
            estimatedGrade=_first_text(data, "estimatedGrade", "grade"),
            notes=_first_text(data, "notes"),
        ),
    )


def _pricing_payload(data: dict[str, Any]) -> dict[str, Any]:
    pricing = data.get("pricing")
    return pricing if isinstance(pricing, dict) else {}


def _portfolio_item_from_row(row: dict[str, Any]) -> PortfolioItem | None:
    item_id = _first_text(row, "id", "item_id", "portfolio_item_id")
    if not item_id:
        return None
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else data.get("pricing")
    merged = {
        **data,
        **{
            key: value
            for key, value in {
                "title": _first_value(row, "title", "item_name", "name"),
                "itemName": _first_value(row, "item_name"),
                "userId": _first_value(row, "user_id", "owner_id"),
                "category": _first_value(row, "category", "item_category"),
                "condition": _first_value(row, "condition"),
                "brand": _first_value(row, "brand"),
                "setName": _first_value(row, "set_name"),
                "series": _first_value(row, "series"),
                "cardNumber": _first_value(row, "card_number"),
                "year": _first_value(row, "year"),
                "currency": _first_value(row, "currency", "display_currency"),
                "displayCurrency": _first_value(row, "display_currency"),
                "needsReview": _first_value(row, "needs_review"),
                "requiresReview": _first_value(row, "requires_review"),
                "reviewedAt": _first_value(row, "reviewed_at"),
                "reviewStatus": _first_value(row, "review_status"),
                "createdAt": _first_value(row, "created_at"),
                "updatedAt": _first_value(row, "updated_at"),
            }.items()
            if value not in (None, "")
        },
    }
    if isinstance(pricing, dict):
        merged["pricing"] = pricing
    return PortfolioItem(id=item_id, data=merged)


def _row_update_from_data(data: dict[str, Any]) -> dict[str, Any]:
    pricing = data.get("pricing") if isinstance(data.get("pricing"), dict) else {}
    value = _price_value({}, pricing) or _estimated_value(data)
    low_value = _estimate_value(pricing, "lowEstimate")
    high_value = _estimate_value(pricing, "highEstimate") or value
    return {
        "data": data,
        "pricing": pricing or None,
        "estimated_value_low": low_value,
        "estimated_value_high": high_value,
        "needs_review": bool(data.get("needsReview") or data.get("requiresReview")),
        "reviewed_at": data.get("reviewedAt"),
        "review_status": data.get("reviewStatus"),
        "updated_at": _utc_now(),
    }


def _confidence(data: dict[str, Any], pricing: dict[str, Any]) -> int | None:
    value = _first_value(
        data,
        pricing,
        "pricingConfidence",
        "confidence",
        "confidenceScore",
    )
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if 0 <= numeric <= 1:
        numeric *= 100
    return max(0, min(100, round(numeric)))


def _price_value(data: dict[str, Any], pricing: dict[str, Any]) -> float | None:
    value = _first_value(
        data,
        pricing,
        "estimatedMarketValue",
        "marketValue",
        "value",
        "price",
    )
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric


def _estimated_value(data: dict[str, Any]) -> float | None:
    try:
        numeric = float(data.get("estimatedValue"))
    except (TypeError, ValueError):
        return None
    return numeric


def _estimate_value(pricing: dict[str, Any], key: str) -> float | None:
    try:
        numeric = float(pricing.get(key))
    except (TypeError, ValueError):
        return None
    return numeric


def _valuation_status(pricing: dict[str, Any]) -> str:
    explicit_status = str(
        pricing.get("valuationStatus") or pricing.get("valuationStrategy") or ""
    ).strip()
    allowed_statuses = {
        "market_estimated",
        "ai_estimated",
        "no_market_match",
        "lookup_failed",
        "provider_not_configured",
        "unavailable",
    }
    if explicit_status in allowed_statuses:
        return explicit_status
    value = _price_value({}, pricing)
    if pricing.get("status") == "available" and value is not None and value > 0:
        return "market_estimated"
    return "unavailable"


def _provider_name(pricing: dict[str, Any]) -> str:
    source = pricing.get("pricingSource") or pricing.get("source")
    if isinstance(source, dict):
        return str(source.get("name") or "Unknown")
    return str(source or "Unknown")


def _first_text(*sources: Any) -> str | None:
    value = _first_value(*sources)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_value(*sources: Any) -> Any:
    dicts = [source for source in sources if isinstance(source, dict)]
    keys = [source for source in sources if isinstance(source, str)]
    for source in dicts:
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _is_stale(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed < datetime.now(timezone.utc) - timedelta(days=STALE_PRICE_DAYS)


def _reason_label(reasons: list[str]) -> str:
    labels = {
        "needs_review": "Needs review",
        "low_confidence": "Low confidence",
        "missing_price": "Missing price",
        "stale_price": "Stale price",
    }
    return labels.get(reasons[0], "Needs review")


def _sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    reason_rank = {
        "missing_price": 0,
        "low_confidence": 1,
        "needs_review": 2,
        "stale_price": 3,
    }
    first_reason = item["reasons"][0]
    confidence = item.get("confidence")
    confidence_rank = confidence if isinstance(confidence, int) else 101
    return (reason_rank.get(first_reason, 9), confidence_rank, item["title"])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bulk_result(item_ids: list[str], action) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for item_id in list(dict.fromkeys([str(value).strip() for value in item_ids if str(value).strip()])):
        try:
            results.append(action(item_id))
        except Exception as error:
            failures.append({"id": item_id, "error": str(error)})
    return {
        "success": not failures,
        "requested": len(item_ids),
        "completed": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }
