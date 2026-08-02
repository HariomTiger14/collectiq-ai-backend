from __future__ import annotations

from typing import Any

from app.schemas.portfolio import PortfolioItem
from app.services.portfolio_service import portfolio_service
from app.services.pricing.admin_review_queue_service import (
    SupabasePricingReviewQueueRepository,
)


class AdminPortfolioService:
    def __init__(
        self,
        *,
        repository: SupabasePricingReviewQueueRepository | None = None,
    ) -> None:
        self._repository = repository or SupabasePricingReviewQueueRepository()

    def list_items(self, *, query: str | None = None, limit: int = 50) -> dict[str, Any]:
        items = (
            self._repository.list_items(limit=max(limit, 200))
            if self._repository.is_configured
            else portfolio_service.list_items()
        )
        normalized_query = (query or "").strip().lower()
        if normalized_query:
            items = [
                item
                for item in items
                if normalized_query in _portfolio_search_text(item)
            ]
        limited = items[:limit]
        return {
            "success": True,
            "query": query or "",
            "count": len(limited),
            "totalCount": len(items),
            "items": [_compact_portfolio_item(item) for item in limited],
        }

    def get_item(self, item_id: str) -> dict[str, Any]:
        item = (
            self._repository.get_item(item_id)
            if self._repository.is_configured
            else portfolio_service.get_item(item_id)
        )
        if item is None:
            raise KeyError(f"Portfolio item {item_id} was not found.")
        return {
            "success": True,
            "item": _compact_portfolio_item(item, include_raw=True),
        }


def _portfolio_search_text(item: PortfolioItem) -> str:
    data = item.data
    values = [
        item.id,
        data.get("title"),
        data.get("itemName"),
        data.get("name"),
        data.get("category"),
        data.get("condition"),
        data.get("userId"),
        data.get("ownerId"),
        data.get("ownerEmail"),
        data.get("pricingAssignee"),
    ]
    return " ".join(str(value) for value in values if value).lower()


def _compact_portfolio_item(item: PortfolioItem, *, include_raw: bool = False) -> dict[str, Any]:
    data = item.data
    pricing = data.get("pricing") if isinstance(data.get("pricing"), dict) else {}
    value = _first_value(data, pricing, "estimatedValue", "estimatedMarketValue", "marketValue", "price")
    payload = {
        "id": item.id,
        "title": _first_text(data, "title", "itemName", "name") or "Untitled collectible",
        "category": _first_text(data, "category", "type") or "Unknown",
        "condition": _first_text(data, "condition") or "Unknown",
        "userId": _first_text(data, "userId", "ownerId", "user_id", "owner_id") or "Unknown",
        "ownerEmail": _first_text(data, "ownerEmail", "email"),
        "price": value,
        "currency": _first_text(data, pricing, "currency", "displayCurrency") or "USD",
        "provider": _provider_name(pricing),
        "confidence": _first_value(data, pricing, "pricingConfidence", "confidence", "confidenceScore"),
        "valuationStatus": _first_text(data, "valuationStatus", "reviewStatus") or "unknown",
        "needsReview": bool(data.get("needsReview") or data.get("requiresReview")),
        "updatedAt": _first_text(data, "updatedAt", "updated_at", "lastUpdated"),
        "createdAt": _first_text(data, "createdAt", "created_at"),
        "assignment": data.get("pricingAssignment") if isinstance(data.get("pricingAssignment"), dict) else {
            "assignee": _first_text(data, "pricingAssignee"),
            "status": _first_text(data, "pricingAssignmentStatus") or "open",
            "updatedAt": _first_text(data, "pricingAssignmentUpdatedAt"),
        },
    }
    if include_raw:
        payload["raw"] = data
        payload["pricing"] = pricing
    return payload


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
