from typing import Any

from pydantic import BaseModel, Field


class ApiAnalysisRequestMetadata(BaseModel):
    imagePath: str
    imageSource: str
    timestamp: str
    requestedCategory: str | None = None
    appVersion: str | None = None
    deviceMetadata: dict[str, str] = Field(default_factory=dict)


class ApiImagePayload(BaseModel):
    fileName: str
    mimeType: str
    sizeBytes: int
    imageSource: str
    localFilePath: str
    imageRole: str | None = None
    slotType: str | None = None
    systemTag: str | None = None
    capturedAt: str | None = None
    base64Image: str | None = None
    base64Preview: str | None = None


class ApiAnalyzeRequest(BaseModel):
    request: ApiAnalysisRequestMetadata
    image: ApiImagePayload | None = None
    images: list[ApiImagePayload] = Field(default_factory=list)


class ApiAlternativeMatchResponse(BaseModel):
    title: str
    category: str
    confidence: int
    reason: str


class ApiReviewResponse(BaseModel):
    primaryMatch: str
    confidenceExplanation: str
    detectionQuality: str
    reasoning: str


class ApiMarketCompResponse(BaseModel):
    source: str
    title: str
    soldPrice: int
    currency: str
    soldDate: str
    condition: str
    url: str | None = None


class ApiMarketSummaryResponse(BaseModel):
    averagePrice: int
    medianPrice: int
    lowPrice: int
    highPrice: int
    salesCount: int
    trendLabel: str
    confidence: int
    lastUpdated: str
    sources: list[str]
    comps: list[ApiMarketCompResponse] = Field(default_factory=list)


class ApiAnalyzeDiagnosticsResponse(BaseModel):
    aiProvider: str
    aiModel: str
    aiLatencyMs: int
    pricingProvider: str
    pricingProviderLatencyMs: int | None = None
    pricingProviderCount: int
    pricingFallbackUsed: bool
    pricingFallbackReason: str | None = None
    pricingCacheStatus: str
    pricingFreshness: str
    pricingProviderAgreement: int | None = None
    pricingVariancePercent: int | None = None
    pricingMedianValue: int | None = None
    pricingOutliersRemoved: int | None = None
    pricingComparableCount: int | None = None
    pricingConfidenceCalculation: str | None = None
    pricingExplanation: str | None = None
    pricingComparableQuality: str | None = None
    valuationStatus: str | None = None
    valuationSource: str | None = None
    confidenceLevel: str
    totalLatencyMs: int


class ApiAnalyzeResponse(BaseModel):
    id: str
    itemName: str
    title: str | None = None
    category: str
    manufacturer: str | None = None
    year: str | None = None
    series: str | None = None
    variant: str | None = None
    estimatedValue: int
    estimated_value: int | None = None
    currency: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    images: list[str] = Field(default_factory=list)
    rawProviderPayload: dict[str, Any] = Field(default_factory=dict)
    faceValue: int | None = None
    estimatedMarketValue: int | None = None
    aiEstimatedValue: int | None = None
    valuationStatus: str
    valuationSource: str
    askingPriceWarning: str | None = None
    valuationConfidence: int | None = None
    lowEstimate: int
    highEstimate: int
    confidence: int
    condition: str
    marketTrend: str
    keyAttributes: dict[str, str]
    aiReview: ApiReviewResponse
    alternatives: list[ApiAlternativeMatchResponse]
    recommendation: str
    marketSummary: ApiMarketSummaryResponse
    comparableSales: list[ApiMarketCompResponse]
    imageUrl: str | None = None
    timestamp: str
    fieldConfidence: dict[str, int] = Field(default_factory=dict)
    confidenceLevel: str
    lowConfidenceReasons: list[str] = Field(default_factory=list)
    imageQualityIssues: list[str] = Field(default_factory=list)
    scanRecommendations: list[str] = Field(default_factory=list)
    diagnostics: ApiAnalyzeDiagnosticsResponse | None = None


class ApiAnalyzeErrorResponse(BaseModel):
    error: dict[str, Any]
