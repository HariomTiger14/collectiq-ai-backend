from typing import Any

from pydantic import BaseModel, Field


class RepriceIdentityRequest(BaseModel):
    title: str
    category: str
    brand: str | None = None
    setName: str | None = None
    series: str | None = None
    cardNumber: str | None = None
    sku: str | None = None
    upc: str | None = None
    condition: str | None = None
    year: str | None = None
    edition: str | None = None
    language: str | None = None
    rarity: str | None = None
    playerOrCharacter: str | None = None
    estimatedGrade: str | None = None
    notes: str | None = None


class RepriceRequest(BaseModel):
    itemId: str | None = None
    previousValue: float | None = None
    previousCurrency: str | None = None
    correctionSource: str = "manual"
    identity: RepriceIdentityRequest


class RepriceComparableSaleResponse(BaseModel):
    source: str
    title: str
    soldPrice: float
    currency: str
    soldDate: str
    condition: str
    url: str | None = None


class RepricePricingResponse(BaseModel):
    status: str
    reasonCode: str | None = None
    displayMessage: str | None = None
    estimatedMarketValue: float | None = None
    lowEstimate: float | None = None
    highEstimate: float | None = None
    currency: str
    displayString: str | None = None
    confidenceScore: float
    pricingConfidence: int
    valuationStrategy: str
    pricingSource: dict[str, Any]
    originalMarketPayload: dict[str, Any] = Field(default_factory=dict)
    matchMetadata: dict[str, Any] = Field(default_factory=dict)
    comparableSales: list[RepriceComparableSaleResponse] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class RepriceResponse(BaseModel):
    success: bool = True
    itemId: str | None = None
    correctionSource: str
    identity: RepriceIdentityRequest
    pricing: RepricePricingResponse
