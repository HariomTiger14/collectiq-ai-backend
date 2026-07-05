from typing import Any

from pydantic import BaseModel, Field


class PortfolioItem(BaseModel):
    id: str
    data: dict[str, Any] = Field(default_factory=dict)


class PortfolioCreateRequest(BaseModel):
    id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class PortfolioListResponse(BaseModel):
    success: bool
    items: list[PortfolioItem]


class PortfolioMutationResponse(BaseModel):
    success: bool
    item: PortfolioItem | None = None
    deletedId: str | None = None
