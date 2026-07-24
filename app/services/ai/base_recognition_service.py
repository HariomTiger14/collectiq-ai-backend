from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AlternativeMatch:
    title: str
    category: str
    confidence: int
    reason: str


@dataclass(frozen=True)
class RecognitionResult:
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
    alternativeMatches: list[AlternativeMatch]
    confidenceExplanation: str
    detectionQuality: str
    aiReasoning: str
    year: str | None = None
    brand: str | None = None
    setName: str | None = None
    series: str | None = None
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
    fieldConfidence: dict[str, int] = field(default_factory=dict)
    confidenceLevel: str | None = None
    lowConfidenceReasons: list[str] = field(default_factory=list)
    imageQualityIssues: list[str] = field(default_factory=list)
    scanRecommendations: list[str] = field(default_factory=list)
    faceValue: int | None = None
    askingPriceWarning: str | None = None
    valuationConfidence: int | None = None


class AIRecognitionProvider(Protocol):
    def recognize(self, image_path: Path) -> RecognitionResult:
        """Recognize a collectible from an uploaded image path."""
        ...


# Backwards-compatible alias for older imports while the backend moves to the
# provider naming used by the real AI foundation.
AIRecognitionService = AIRecognitionProvider

# Product-facing provider alias used by the backend analyze endpoint roadmap.
AiProvider = AIRecognitionProvider
