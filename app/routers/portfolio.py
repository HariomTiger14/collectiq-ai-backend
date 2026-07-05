from fastapi import APIRouter

from app.schemas.portfolio import (
    PortfolioCreateRequest,
    PortfolioListResponse,
    PortfolioMutationResponse,
)
from app.services.portfolio_service import portfolio_service


router = APIRouter(prefix="/portfolio", tags=["Portfolio"])


@router.get("", response_model=PortfolioListResponse)
async def get_portfolio() -> PortfolioListResponse:
    return PortfolioListResponse(success=True, items=portfolio_service.list_items())


@router.post("", response_model=PortfolioMutationResponse)
async def create_portfolio_item(
    request: PortfolioCreateRequest,
) -> PortfolioMutationResponse:
    item = portfolio_service.add_item(request)
    return PortfolioMutationResponse(success=True, item=item)


@router.delete("/{item_id}", response_model=PortfolioMutationResponse)
async def delete_portfolio_item(item_id: str) -> PortfolioMutationResponse:
    portfolio_service.delete_item(item_id)
    return PortfolioMutationResponse(success=True, deletedId=item_id)
