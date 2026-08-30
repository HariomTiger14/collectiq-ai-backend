from pydantic import BaseModel, Field


class CatalogSearchPricing(BaseModel):
    currency: str = "USD"
    # Set only when a non-USD display currency was requested and conversion
    # was actually applied -- None means these values are already in their
    # native/source currency (the common case: PriceCharting/KicksDB data
    # is USD, and no conversion was requested). Mirrors the same "honesty
    # first, no fabricated FX unless asked for" pattern already established
    # for scan pricing (see currency_conversion.py's convert_pricing_result
    # and originalCurrency on PricingResult).
    originalCurrency: str | None = None
    marketValue: float | None = None
    lowEstimate: float | None = None
    highEstimate: float | None = None
    loosePrice: float | None = None
    cibPrice: float | None = None
    newPrice: float | None = None
    gradedPrice: float | None = None


class CatalogImage(BaseModel):
    """One image of a catalog item, for surfaces that can show more than one.

    `label` names the view when an item has several ("Obverse"/"Reverse"
    for coins, "Angle 2" for sneakers); `credit` carries a required
    photo-credit line when the source demands one (contributor-photo
    catalogs like Numista), and must be displayed alongside the image
    wherever it is set.

    `attributionRequired` separates "nice to show" from "must show".
    Public-domain images carry a credit for provenance, but for a
    CC BY / CC BY-SA image naming the author is a CONDITION OF THE
    LICENCE -- displaying it is not optional, and a client that hides
    the credit to save space would be infringing. Clients must render
    `credit` wherever such an image appears, linking to
    `attributionUrl` when present.
    """

    url: str
    label: str | None = None
    credit: str | None = None
    attributionRequired: bool = False
    attributionUrl: str | None = None


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
    # Additional views of the same item, detail surfaces only. Empty for
    # most items (one photo is the norm); populated where a source really
    # has several -- sneakers (KicksDB/StockX gallery), coins
    # (obverse/reverse). imageUrl stays the primary/thumbnail image and is
    # always the first entry when this is non-empty, so clients that
    # ignore this field are unaffected.
    images: list[CatalogImage] = Field(default_factory=list)
    pricing: CatalogSearchPricing = Field(default_factory=CatalogSearchPricing)


class CatalogSearchResponse(BaseModel):
    success: bool = True
    query: str
    count: int
    results: list[CatalogSearchResult]


class MarketplaceListing(BaseModel):
    title: str
    price: float
    currency: str
    condition: str = ""
    url: str
    source: str = "eBay"
    # Sneaker (KicksDB/StockX) listings only -- per-size market depth.
    # None for eBay/PriceCharting listings, so existing clients and cached
    # rows deserialize unchanged.
    size: str | None = None
    totalAsks: int | None = None
    salesLast30Days: int | None = None


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
    marketplaceListings: list[MarketplaceListing] = Field(default_factory=list)
