"""Portfolio schemas."""

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


class PortfolioItemCreate(BaseModel):
    """Request body for creating a portfolio item."""

    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    confidence: int = Field(ge=0, le=100)
    estimated_value: int = Field(ge=0, alias="estimatedValue")
    condition: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)

    model_config = {"populate_by_name": True}


class PortfolioItemResponse(PortfolioItemCreate):
    """Portfolio item returned by the API."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="createdAt",
    )


class PortfolioResponse(BaseModel):
    """Response containing portfolio items and summary values."""

    items: list[PortfolioItemResponse]
    total_items: int = Field(alias="totalItems")
    total_value: int = Field(alias="totalValue")

    model_config = {"populate_by_name": True}
