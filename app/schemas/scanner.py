from pydantic import BaseModel


class AlternativeMatchResponse(BaseModel):
    title: str
    category: str
    confidence: int
    reason: str


class PricingResponse(BaseModel):
    estimatedMarketValue: int
    lowEstimate: int
    highEstimate: int
    currency: str
    pricingSource: str
    pricingConfidence: int
    lastUpdated: str


class ScannerAnalysisResponse(BaseModel):
    success: bool
    filename: str
    imageUrl: str
    title: str
    category: str
    confidence: int
    estimatedValue: int
    condition: str
    recommendation: str
    description: str
    detectedObjects: list[str]
    aiProvider: str
    processingTimeMs: int
    primaryMatch: str
    alternativeMatches: list[AlternativeMatchResponse]
    confidenceExplanation: str
    detectionQuality: str
    aiReasoning: str
    year: str | None = None
    brand: str | None = None
    setName: str | None = None
    series: str | None = None
    manufacturer: str | None = None
    estimated_value_low: int | None = None
    estimated_value_high: int | None = None
    cardNumber: str | None = None
    playerOrCharacter: str | None = None
    rarity: str | None = None
    estimatedGrade: str | None = None
    language: str | None = None
    edition: str | None = None
    country: str | None = None
    mint: str | None = None
    material: str | None = None
    notes: str | None = None
    pricing: PricingResponse
