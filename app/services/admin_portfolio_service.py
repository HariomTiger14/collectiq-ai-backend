from __future__ import annotations

from datetime import datetime, timezone
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

    def list_items(self, *, query: str | None = None, limit: int = 50, user_id: str | None = None) -> dict[str, Any]:
        items = (
            self._repository.list_items(limit=max(limit, 200), user_id=user_id)
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

    def update_item(self, item_id: str, updates: dict[str, Any], *, actor: str = "admin") -> dict[str, Any]:
        current = (
            self._repository.get_item(item_id)
            if self._repository.is_configured
            else portfolio_service.get_item(item_id)
        )
        if current is None:
            raise KeyError(f"Portfolio item {item_id} was not found.")

        update_data = _editable_portfolio_updates(updates)
        if not update_data:
            return {
                "success": True,
                "item": _compact_portfolio_item(current, include_raw=True),
                "updated": False,
                "message": "No editable fields were supplied.",
            }
        update_data["adminLastEditedAt"] = datetime.now(timezone.utc).isoformat()
        update_data["adminLastEditedBy"] = actor
        item = (
            self._repository.update_item_data(item_id, update_data)
            if self._repository.is_configured
            else portfolio_service.update_item_data(item_id, update_data)
        )
        if item is None:
            raise KeyError(f"Portfolio item {item_id} was not found.")
        return {
            "success": True,
            "updated": True,
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
        "adminNotes": _first_text(data, "adminNotes", "adminNote"),
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


def _editable_portfolio_updates(updates: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    text_fields = {
        "category": "category",
        "condition": "condition",
        "adminNotes": "adminNotes",
        "valuationStatus": "valuationStatus",
        "reviewStatus": "reviewStatus",
        "pricingProvider": "pricingProvider",
        "currency": "currency",
    }
    for source_key, target_key in text_fields.items():
        value = updates.get(source_key)
        if value is not None:
            cleaned[target_key] = str(value).strip()
    if "confidence" in updates and updates["confidence"] not in (None, ""):
        cleaned["pricingConfidence"] = _bounded_number(updates["confidence"], minimum=0, maximum=100)
    if "price" in updates and updates["price"] not in (None, ""):
        price = _bounded_number(updates["price"], minimum=0)
        cleaned["estimatedMarketValue"] = price
        pricing = updates.get("pricing") if isinstance(updates.get("pricing"), dict) else {}
        cleaned["pricing"] = {
            **pricing,
            "estimatedMarketValue": price,
            "currency": cleaned.get("currency") or pricing.get("currency") or "USD",
            "pricingConfidence": cleaned.get("pricingConfidence"),
            "pricingSource": {"name": cleaned.get("pricingProvider") or pricing.get("pricingProvider") or "admin_override"},
        }
    return {key: value for key, value in cleaned.items() if value not in (None, "")}


def _bounded_number(value: Any, *, minimum: float = 0, maximum: float | None = None) -> float:
    number = float(value)
    if number < minimum:
        number = minimum
    if maximum is not None and number > maximum:
        number = maximum
    return number


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
