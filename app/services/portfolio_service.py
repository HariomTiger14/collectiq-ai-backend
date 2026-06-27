"""In-memory portfolio service."""

from fastapi import HTTPException, status

from app.schemas.portfolio import (
    PortfolioItemCreate,
    PortfolioItemResponse,
    PortfolioResponse,
)


class PortfolioService:
    """Service that manages portfolio items in memory."""

    def __init__(self) -> None:
        """Initialize empty in-memory storage."""
        self._items: dict[str, PortfolioItemResponse] = {}

    def get_portfolio(self) -> PortfolioResponse:
        """Return all saved portfolio items with summary totals."""
        items = list(self._items.values())
        return PortfolioResponse(
            items=items,
            total_items=len(items),
            total_value=sum(item.estimated_value for item in items),
        )

    def save_item(self, item: PortfolioItemCreate) -> PortfolioItemResponse:
        """Save a portfolio item in memory."""
        saved_item = PortfolioItemResponse(**item.model_dump())
        self._items[saved_item.id] = saved_item
        return saved_item

    def delete_item(self, item_id: str) -> None:
        """Delete a portfolio item by id."""
        if item_id not in self._items:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Portfolio item not found.",
            )

        del self._items[item_id]
