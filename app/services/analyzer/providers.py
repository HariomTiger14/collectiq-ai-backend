from pathlib import Path
from typing import Protocol

from app.services.ai.base_recognition_service import AlternativeMatch, RecognitionResult
from app.services.ai.mock_recognition_service import MockRecognitionProvider
from app.services.ai.openai_recognition_provider import OpenAIRecognitionProvider


class BackendAnalyzerProvider(Protocol):
    provider_name: str

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        """Analyze a backend contract payload with server-side credentials."""
        ...


class MockAnalyzerProvider:
    provider_name = "mock"

    def __init__(self, delegate: MockRecognitionProvider | None = None) -> None:
        self._delegate = delegate or MockRecognitionProvider()

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        return _mock_result_for(request_metadata=request_metadata, image_payload=image_payload)


class OpenAIAnalyzerProvider:
    provider_name = "openai"

    def __init__(self, delegate: OpenAIRecognitionProvider | None = None) -> None:
        self._delegate = delegate or OpenAIRecognitionProvider()

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        return self._delegate.recognize_api_payload(
            request_metadata=request_metadata,
            image_payload=image_payload,
        )


class GeminiAnalyzerProvider:
    provider_name = "gemini"

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        raise NotImplementedError(
            "Gemini analyzer provider is a backend-only placeholder."
        )


def recognize_with_legacy_provider(
    provider,
    *,
    request_metadata: dict,
    image_payload: dict,
) -> RecognitionResult:
    if hasattr(provider, "recognize_api_payload"):
        return provider.recognize_api_payload(
            request_metadata=request_metadata,
            image_payload=image_payload,
        )

    return provider.recognize(Path(image_payload.get("localFilePath") or "uploads/mock.jpg"))


def _mock_result_for(*, request_metadata: dict, image_payload: dict) -> RecognitionResult:
    requested_category = str(request_metadata.get("requestedCategory") or "").lower()
    file_name = str(image_payload.get("fileName") or "").lower()
    haystack = f"{requested_category} {file_name}"

    if any(term in haystack for term in ["coin", "dollar", "mint"]):
        return _coin_result()
    if any(term in haystack for term in ["toy", "figure", "transformer", "optimus"]):
        return _toy_result()
    if any(term in haystack for term in ["book", "guide", "manual", "magazine"]):
        return _book_result()
    if any(term in haystack for term in ["memorabilia", "signed", "autograph", "jersey"]):
        return _memorabilia_result()
    return _trading_card_result()


def _alternatives(*matches: tuple[str, str, int, str]) -> list[AlternativeMatch]:
    return [
        AlternativeMatch(
            title=title,
            category=category,
            confidence=confidence,
            reason=reason,
        )
        for title, category, confidence, reason in matches
    ]


def _trading_card_result() -> RecognitionResult:
    return RecognitionResult(
        title="1999 Pokemon Charizard Holo",
        category="Trading Card",
        confidence=94,
        estimatedValue=1850,
        condition="Near Mint",
        recommendation="Review holo surface and centering before saving or listing.",
        description="A realistic mock match for a vintage holographic trading card.",
        detectedObjects=["card", "pokemon", "charizard", "holographic"],
        aiProvider="mock",
        processingTimeMs=132,
        primaryMatch="1999 Pokemon Charizard Holo",
        alternativeMatches=_alternatives(
            ("2016 Pokemon Evolutions Charizard", "Trading Card", 72, "Similar artwork cues."),
            ("Pokemon Charizard Promo", "Trading Card", 65, "Character match is plausible."),
            ("Pokemon Expedition Charizard", "Trading Card", 58, "Similar subject, different set."),
        ),
        confidenceExplanation="High confidence from visible character, card frame, and holo cues.",
        detectionQuality="Good - card border and artwork are readable.",
        aiReasoning="Metadata and image hints match a collectible trading card profile.",
        year="1999",
        brand="Pokemon",
        setName="Base Set",
        series="Pokemon TCG",
        cardNumber="4/102",
        playerOrCharacter="Charizard",
        rarity="Holo Rare",
        estimatedGrade="PSA 8-9",
        language="English",
        edition="Unlimited",
        country="United States",
        material="Cardstock",
        notes="Confirm edition, holo scratches, and back centering.",
    )


def _coin_result() -> RecognitionResult:
    return RecognitionResult(
        title="1921 Morgan Silver Dollar",
        category="Coin",
        confidence=88,
        estimatedValue=145,
        condition="Very Fine",
        recommendation="Confirm weight, diameter, and mint mark before valuation.",
        description="A realistic mock match for a US silver dollar collectible.",
        detectedObjects=["coin", "silver", "morgan dollar"],
        aiProvider="mock",
        processingTimeMs=118,
        primaryMatch="1921 Morgan Silver Dollar",
        alternativeMatches=_alternatives(
            ("Peace Silver Dollar", "Coin", 70, "Similar size and silver tone."),
            ("1878 Morgan Silver Dollar", "Coin", 66, "Same type, different date."),
            ("American Silver Eagle", "Coin", 52, "Silver coin appearance overlaps."),
        ),
        confidenceExplanation="Confidence comes from round silver profile and Morgan design cues.",
        detectionQuality="Fair - reflective surfaces can obscure date and mint mark.",
        aiReasoning="The request metadata aligns with a collectible coin workflow.",
        year="1921",
        brand="United States Mint",
        series="Morgan Dollar",
        country="United States",
        mint="Philadelphia",
        material="90% silver, 10% copper",
        estimatedGrade="VF",
        notes="Do not clean; inspect mint mark and rim wear.",
    )


def _toy_result() -> RecognitionResult:
    return RecognitionResult(
        title="Transformers Optimus Prime G1 Figure",
        category="Toy/Figure",
        confidence=85,
        estimatedValue=260,
        condition="Good",
        recommendation="Check for trailer, fists, blaster, and sticker condition.",
        description="A realistic mock match for a vintage robot figure.",
        detectedObjects=["toy", "figure", "robot", "truck"],
        aiProvider="mock",
        processingTimeMs=140,
        primaryMatch="Transformers Optimus Prime G1 Figure",
        alternativeMatches=_alternatives(
            ("Optimus Prime Reissue", "Toy/Figure", 76, "Same character design."),
            ("Ultra Magnus G1 Figure", "Toy/Figure", 59, "Truck robot silhouette overlaps."),
            ("Powermaster Optimus Prime", "Toy/Figure", 54, "Character match differs by mold."),
        ),
        confidenceExplanation="Moderate confidence from red truck and robot-form cues.",
        detectionQuality="Fair - accessory completeness is not fully visible.",
        aiReasoning="The metadata points to a toy or figure and the mock fixture models that path.",
        year="1984",
        brand="Hasbro",
        series="Transformers Generation 1",
        playerOrCharacter="Optimus Prime",
        rarity="Vintage Figure",
        estimatedGrade="Loose Good",
        edition="G1",
        country="Japan",
        material="Plastic and die-cast metal",
        notes="Accessory completeness materially affects value.",
    )


def _book_result() -> RecognitionResult:
    return RecognitionResult(
        title="Pokemon Red and Blue Prima Strategy Guide",
        category="Book/Guide",
        confidence=82,
        estimatedValue=95,
        condition="Very Good",
        recommendation="Inspect spine, pages, poster inserts, and edition marks.",
        description="A realistic mock match for a collectible gaming guide.",
        detectedObjects=["book", "guide", "pokemon", "paperback"],
        aiProvider="mock",
        processingTimeMs=126,
        primaryMatch="Pokemon Red and Blue Prima Strategy Guide",
        alternativeMatches=_alternatives(
            ("Pokemon Yellow Strategy Guide", "Book/Guide", 68, "Same franchise and format."),
            ("Nintendo Power Pokemon Guide", "Book/Guide", 61, "Guide format overlaps."),
            ("Pokemon Stadium Guide", "Book/Guide", 55, "Similar gaming guide era."),
        ),
        confidenceExplanation="Moderate confidence from title/category metadata and guide-like format.",
        detectionQuality="Good - cover/title area appears sufficient for mock analysis.",
        aiReasoning="The request metadata indicates a collectible book or guide.",
        year="1998",
        brand="Prima Games",
        series="Pokemon Guides",
        language="English",
        edition="First Edition",
        country="United States",
        material="Printed paper",
        notes="Poster or map inserts can affect value.",
    )


def _memorabilia_result() -> RecognitionResult:
    return RecognitionResult(
        title="Signed Team Jersey Memorabilia",
        category="Memorabilia",
        confidence=79,
        estimatedValue=320,
        condition="Good",
        recommendation="Verify signature authenticity and provenance documentation.",
        description="A realistic mock match for signed sports or entertainment memorabilia.",
        detectedObjects=["memorabilia", "signature", "jersey", "framed item"],
        aiProvider="mock",
        processingTimeMs=150,
        primaryMatch="Signed Team Jersey Memorabilia",
        alternativeMatches=_alternatives(
            ("Unsigned Team Jersey", "Memorabilia", 64, "Same item type without signature proof."),
            ("Signed Sports Photo", "Memorabilia", 58, "Autograph context overlaps."),
            ("Framed Display Jersey", "Memorabilia", 55, "Presentation style is similar."),
        ),
        confidenceExplanation="Moderate confidence; authenticity requires external verification.",
        detectionQuality="Fair - signature and certificate details may need close-ups.",
        aiReasoning="The request metadata indicates a memorabilia workflow.",
        brand="Authenticated Collectibles",
        series="Signed Memorabilia",
        rarity="Signed",
        estimatedGrade="Display Good",
        material="Fabric and frame materials",
        notes="COA, signer identity, and item condition drive value.",
    )
