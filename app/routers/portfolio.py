"""Portfolio routes."""

from fastapi import APIRouter, Depends, status

from app.core.dependencies import get_portfolio_service
from app.schemas.portfolio import (
    PortfolioItemCreate,
    PortfolioItemResponse,
    PortfolioResponse,
)
from app.services.portfolio_service import PortfolioService

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("", response_model=PortfolioResponse)
def get_portfolio(
    portfolio_service: PortfolioService = Depends(get_portfolio_service),
) -> PortfolioResponse:
    """Return all in-memory portfolio items."""
    return portfolio_service.get_portfolio()


@router.post(
    "",
    response_model=PortfolioItemResponse,
    status_code=status.HTTP_201_CREATED,
)
def save_portfolio_item(
    item: PortfolioItemCreate,
    portfolio_service: PortfolioService = Depends(get_portfolio_service),
) -> PortfolioItemResponse:
    """Save an item to the in-memory portfolio."""
    return portfolio_service.save_item(item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_portfolio_item(
    item_id: str,
    portfolio_service: PortfolioService = Depends(get_portfolio_service),
) -> None:
    """Delete an item from the in-memory portfolio."""
    portfolio_service.delete_item(item_id)
