from pydantic import BaseModel, Field


class EbayMetadataResult(BaseModel):
    itemId: str
    title: str
    source: str = "eBay Browse API"
    dataUse: str = "metadata_only"
    valuationStatus: str = "not_valuation"
    categoryId: str | None = None
    categoryName: str | None = None
    condition: str | None = None
    itemWebUrl: str | None = None
    imageUrl: str | None = None
    itemLocationCountry: str | None = None
    itemCreationDate: str | None = None
    itemEndDate: str | None = None
    itemAspects: dict[str, list[str]] = Field(default_factory=dict)
    attribution: str = "Metadata from eBay Browse API. Active listing prices are not used for PackLox valuations."


class EbayMetadataSearchResponse(BaseModel):
    success: bool = True
    query: str
    count: int
    results: list[EbayMetadataResult]
    dataUse: str = "metadata_only"
    valuationStatus: str = "not_valuation"
    attribution: str = "Metadata from eBay Browse API. Active listing prices are not used for PackLox valuations."

