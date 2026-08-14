from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.schemas.portfolio import PortfolioItem
from app.services.portfolio_service import portfolio_service
from app.services.pricing.admin_review_queue_service import (
    SupabasePricingReviewQueueRepository,
)

# Caps the per-request fan-out of single-user Supabase Auth lookups used to
# fill in an email for owners with no collector_profiles display name.
_MAX_OWNER_EMAIL_LOOKUPS = 20


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
        owner_names: dict[str, str] = {}
        if self._repository.is_configured:
            owner_ids = [str(item.data.get("userId")) for item in limited if item.data.get("userId")]
            owner_names = self._repository.batch_owner_display_names(owner_ids)
            # Not every user sets a display name. For the (usually small)
            # remainder, fall back to their real email — one request per
            # distinct owner, since Supabase Auth has no bulk-by-ids lookup.
            # Capped so a page full of strangers can't turn into an
            # unbounded fan-out of admin API calls.
            missing_ids = list(dict.fromkeys(oid for oid in owner_ids if oid not in owner_names))
            for owner_id in missing_ids[:_MAX_OWNER_EMAIL_LOOKUPS]:
                email = self._repository.get_user_email(owner_id)
                if email:
                    owner_names[owner_id] = email
        return {
            "success": True,
            "query": query or "",
            "count": len(limited),
            "totalCount": len(items),
            "items": [
                _compact_portfolio_item(item, owner_display_name=owner_names.get(str(item.data.get("userId"))))
                for item in limited
            ],
        }

    def get_item(self, item_id: str) -> dict[str, Any]:
        item = (
            self._repository.get_item(item_id)
            if self._repository.is_configured
            else portfolio_service.get_item(item_id)
        )
        if item is None:
            raise KeyError(f"Portfolio item {item_id} was not found.")
        valuation_history = (
            self._repository.list_valuation_history_for_item(item_id)
            if self._repository.is_configured
            else []
        )
        # list_items() resolves a real name/email for the Owner column, but
        # this single-item lookup never did the same -- the item detail page
        # showed a raw UUID even when the exact same item's row on the list
        # page showed a real name.
        owner_display_name = None
        if self._repository.is_configured:
            user_id = item.data.get("userId")
            if user_id:
                owner_display_name = self._repository.batch_owner_display_names([str(user_id)]).get(str(user_id))
                if not owner_display_name:
                    owner_display_name = self._repository.get_user_email(str(user_id))
        return {
            "success": True,
            "item": _compact_portfolio_item(item, include_raw=True, owner_display_name=owner_display_name),
            "valuationHistory": [_compact_valuation_snapshot(row) for row in valuation_history],
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


def _compact_portfolio_item(
    item: PortfolioItem, *, include_raw: bool = False, owner_display_name: str | None = None,
) -> dict[str, Any]:
    data = item.data
    pricing = data.get("pricing") if isinstance(data.get("pricing"), dict) else {}
    value = _first_value(data, pricing, "estimatedValue", "estimatedMarketValue", "marketValue", "price")
    payload = {
        "id": item.id,
        "title": _first_text(data, "title", "itemName", "name") or "Untitled collectible",
        "category": _first_text(data, "category", "type") or "Unknown",
        "condition": _first_text(data, "condition") or "Unknown",
        "userId": _first_text(data, "userId", "ownerId", "user_id", "owner_id") or "Unknown",
        # There's no email column on this table (email lives only in
        # Supabase Auth) — ownerEmail is really "owner display name" when
        # sourced from collector_profiles, kept under its original field
        # name so the frontend doesn't need to change.
        "ownerEmail": owner_display_name or _first_text(data, "ownerEmail", "email"),
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
    # Financial/workflow fields (price, currency, confidence, pricingProvider,
    # valuationStatus, reviewStatus) are deliberately not editable here — see
    # the comment on PortfolioItemUpdateRequest for why.
    cleaned: dict[str, Any] = {}
    text_fields = {
        "category": "category",
        "condition": "condition",
        "adminNotes": "adminNotes",
    }
    for source_key, target_key in text_fields.items():
        value = updates.get(source_key)
        if value is not None:
            cleaned[target_key] = str(value).strip()
    return {key: value for key, value in cleaned.items() if value not in (None, "")}


def _compact_valuation_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "valueAud": row.get("value_aud"),
        "displayString": row.get("display_string"),
        "valuationStatus": row.get("valuation_status"),
        "valuationStrategy": row.get("valuation_strategy"),
        "pricedAt": row.get("priced_at"),
    }


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
