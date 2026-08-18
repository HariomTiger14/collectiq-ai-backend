from pydantic import BaseModel, Field


class CatalogSearchPricing(BaseModel):
    currency: str = "USD"
    marketValue: float | None = None
    lowEstimate: float | None = None
    highEstimate: float | None = None
    loosePrice: float | None = None
    cibPrice: float | None = None
    newPrice: float | None = None
    gradedPrice: float | None = None


class CatalogSearchResult(BaseModel):
    id: str
    title: str
    category: str
    source: str = "PriceCharting"
    setName: str | None = None
    identifier: str | None = None
    productUrl: str | None = None
    sourceFile: str | None = None
    confidence: float | None = None
    attribution: str = "Pricing data by PriceCharting"
    lastUpdated: str | None = None
    imageUrl: str | None = None
    # Link-only, never rendered inline: the same publisher-sourced image
    # URL detail() attaches as imageUrl, but exposed here strictly so the
    # client can open it in an external/in-app browser tab from the open
    # catalog search surface -- never as an <img>/Image.network source. See
    # CatalogSearchService.search()'s enrichment pass.
    externalImageUrl: str | None = None
    pricing: CatalogSearchPricing = Field(default_factory=CatalogSearchPricing)


class CatalogSearchResponse(BaseModel):
    success: bool = True
    query: str
    count: int
    results: list[CatalogSearchResult]


class CatalogHistoryPoint(BaseModel):
    validFrom: str
    validTo: str | None = None
    isCurrent: bool = False
    sourceFile: str | None = None
    sourceDownloadedAt: str | None = None
    pricing: CatalogSearchPricing = Field(default_factory=CatalogSearchPricing)


class CatalogDetailResponse(BaseModel):
    success: bool = True
    result: CatalogSearchResult
    history: list[CatalogHistoryPoint] = Field(default_factory=list)
