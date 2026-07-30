from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

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


class AdminPricingReviewQueueService:
    def list_queue(self, *, reason: str = "all", limit: int = 50) -> dict[str, Any]:
        queue_items = [
            review_item
            for item in portfolio_service.list_items()
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
        item = portfolio_service.update_item_data(
            item_id,
            {
                "needsReview": False,
                "reviewedAt": _utc_now(),
                "reviewStatus": "reviewed",
            },
        )
        if item is None:
            raise ReviewQueueItemNotFoundError(f"Portfolio item {item_id} was not found.")
        return {
            "success": True,
            "itemId": item_id,
            "reviewStatus": "reviewed",
        }

    def retry_pricing(self, item_id: str) -> dict[str, Any]:
        item = portfolio_service.get_item(item_id)
        if item is None:
            raise ReviewQueueItemNotFoundError(f"Portfolio item {item_id} was not found.")
        request = _reprice_request_from_item(item)
        response = RepriceService().reprice(request)
        portfolio_service.update_item_data(
            item_id,
            {
                "pricing": response.pricing.model_dump(mode="json"),
                "needsReview": response.pricing.pricingConfidence < LOW_CONFIDENCE_THRESHOLD
                or response.pricing.status != "available",
                "lastPricingRetryAt": _utc_now(),
                "reviewStatus": "pricing_retried",
            },
        )
        return {
            "success": True,
            "itemId": item_id,
            "pricing": response.pricing.model_dump(mode="json"),
        }


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
