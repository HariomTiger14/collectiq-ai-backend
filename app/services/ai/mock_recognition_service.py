import random
from pathlib import Path

from app.services.ai.base_recognition_service import (
    AIRecognitionProvider,
    AlternativeMatch,
    RecognitionResult,
)


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


MOCK_COLLECTIBLES = [
    RecognitionResult(
        title="1999 Pokemon Charizard Holo",
        category="Pokemon Card",
        confidence=94,
        estimatedValue=1850,
        condition="Near Mint",
        recommendation="Consider professional grading before selling.",
        description="Likely a Base Set Charizard holographic card with strong collector demand.",
        detectedObjects=["Card", "Pokemon", "Charizard", "Holographic"],
        aiProvider="mock",
        processingTimeMs=132,
        primaryMatch="1999 Pokemon Charizard Holo",
        alternativeMatches=_alternatives(
            (
                "2002 Pokemon Expedition Charizard",
                "Pokemon Card",
                72,
                "Similar fire-type character and card framing.",
            ),
            (
                "2016 Pokemon Evolutions Charizard",
                "Pokemon Card",
                68,
                "Modern reprint shares the same artwork cues.",
            ),
            (
                "Pokemon Charizard Promo Card",
                "Pokemon Card",
                61,
                "Character match is strong, but set details differ.",
            ),
        ),
        confidenceExplanation="High confidence from the character artwork, holo pattern, and trading card proportions.",
        detectionQuality="Good - image appears sharp enough to inspect artwork and border details.",
        aiReasoning="The image contains a dragon-like Pokemon, orange/red palette, card border, and holographic finish consistent with Charizard collector cards.",
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
        notes="Verify set stamp, holo surface, and back centering before grading.",
    ),
    RecognitionResult(
        title="1986 Fleer Michael Jordan Rookie",
        category="Sports Card",
        confidence=89,
        estimatedValue=4200,
        condition="Excellent",
        recommendation="Protect in a rigid sleeve and verify centering with a grader.",
        description="Basketball rookie card style match with high resale interest.",
        detectedObjects=["Card", "Basketball", "Rookie", "Chicago"],
        aiProvider="mock",
        processingTimeMs=148,
        primaryMatch="1986 Fleer Michael Jordan Rookie",
        alternativeMatches=_alternatives(
            (
                "1987 Fleer Michael Jordan",
                "Sports Card",
                74,
                "Same player and era, but border/year details may differ.",
            ),
            (
                "1986 Fleer Sticker Michael Jordan",
                "Sports Card",
                69,
                "Similar subject with sticker-style variation possible.",
            ),
            (
                "1990 Hoops Michael Jordan",
                "Sports Card",
                55,
                "Player match is plausible, but design is less consistent.",
            ),
        ),
        confidenceExplanation="Strong confidence from basketball card layout, player pose, and Chicago-era visual cues.",
        detectionQuality="Good - corners and centering are partly visible, but fine print is not fully readable.",
        aiReasoning="The card resembles a vintage basketball issue with Jordan-like subject matter and period-correct framing.",
        year="1986",
        brand="Fleer",
        setName="Fleer Basketball",
        series="NBA",
        cardNumber="57",
        playerOrCharacter="Michael Jordan",
        rarity="Rookie Card",
        estimatedGrade="PSA 6-7",
        language="English",
        edition="Base",
        country="United States",
        material="Cardstock",
        notes="Centering and edge wear are major value drivers for this card.",
    ),
    RecognitionResult(
        title="1921 Morgan Silver Dollar",
        category="Coin",
        confidence=87,
        estimatedValue=145,
        condition="Very Fine",
        recommendation="Store in a non-PVC coin flip and avoid cleaning the surface.",
        description="Classic US silver dollar with visible Morgan profile and eagle reverse cues.",
        detectedObjects=["Coin", "Silver", "Morgan Dollar"],
        aiProvider="mock",
        processingTimeMs=118,
        primaryMatch="1921 Morgan Silver Dollar",
        alternativeMatches=_alternatives(
            (
                "Peace Silver Dollar",
                "Coin",
                70,
                "Similar silver dollar size and metal tone.",
            ),
            (
                "1878 Morgan Silver Dollar",
                "Coin",
                66,
                "Same coin type, but date details are uncertain.",
            ),
            (
                "American Silver Eagle",
                "Coin",
                52,
                "Silver coin appearance overlaps, but design cues differ.",
            ),
        ),
        confidenceExplanation="Confidence is based on round silver coin shape, portrait profile, and Morgan dollar design signals.",
        detectionQuality="Fair - reflective surfaces make date and mint details harder to confirm.",
        aiReasoning="The visible profile and eagle motifs align with US Morgan dollar imagery, though close-up date inspection would improve certainty.",
        year="1921",
        brand="United States Mint",
        series="Morgan Dollar",
        country="United States",
        mint="Philadelphia",
        material="90% silver, 10% copper",
        estimatedGrade="VF",
        notes="Do not clean the coin; confirm mint mark and weight for authenticity.",
    ),
    RecognitionResult(
        title="Amazing Spider-Man #300",
        category="Comic",
        confidence=91,
        estimatedValue=900,
        condition="Fine",
        recommendation="Bag, board, and inspect spine ticks before submitting for grading.",
        description="Comic cover appears consistent with the first full Venom appearance.",
        detectedObjects=["Comic", "Spider-Man", "Venom", "Cover"],
        aiProvider="mock",
        processingTimeMs=156,
        primaryMatch="Amazing Spider-Man #300",
        alternativeMatches=_alternatives(
            (
                "Amazing Spider-Man #316",
                "Comic",
                73,
                "Venom cover cues are similar but issue layout differs.",
            ),
            (
                "Amazing Spider-Man #252",
                "Comic",
                64,
                "Black suit Spider-Man visual overlap.",
            ),
            (
                "Web of Spider-Man #1",
                "Comic",
                58,
                "Spider-Man cover composition shares related visual cues.",
            ),
        ),
        confidenceExplanation="High confidence from the cover composition, character colors, and comic layout.",
        detectionQuality="Good - cover art is readable, though small issue text could be clearer.",
        aiReasoning="The scan shows comic cover proportions and Spider-Man/Venom style artwork associated with ASM #300.",
        year="1988",
        brand="Marvel Comics",
        setName="Amazing Spider-Man",
        series="Amazing Spider-Man",
        cardNumber="#300",
        playerOrCharacter="Spider-Man / Venom",
        rarity="Key Issue",
        estimatedGrade="Fine",
        language="English",
        edition="First Print",
        country="United States",
        material="Paper",
        notes="First full Venom appearance; spine ticks and page color affect grade.",
    ),
    RecognitionResult(
        title="Transformers Optimus Prime G1 Figure",
        category="Toy/Figure",
        confidence=84,
        estimatedValue=260,
        condition="Good",
        recommendation="Check for original accessories to improve resale value.",
        description="Vintage transforming robot figure with recognizable red cab styling.",
        detectedObjects=["Toy", "Figure", "Robot", "Optimus Prime"],
        aiProvider="mock",
        processingTimeMs=140,
        primaryMatch="Transformers Optimus Prime G1 Figure",
        alternativeMatches=_alternatives(
            (
                "Transformers Optimus Prime Reissue",
                "Toy/Figure",
                76,
                "Same character design with possible later production.",
            ),
            (
                "Transformers Ultra Magnus G1 Figure",
                "Toy/Figure",
                59,
                "Truck/robot silhouette overlaps in vintage line.",
            ),
            (
                "Transformers Powermaster Optimus Prime",
                "Toy/Figure",
                54,
                "Character match is plausible but body proportions differ.",
            ),
        ),
        confidenceExplanation="Confidence comes from the robot form, red cab elements, and vintage toy proportions.",
        detectionQuality="Fair - accessory completeness and manufacturing marks are not fully visible.",
        aiReasoning="The figure has the red/blue truck robot cues commonly associated with Optimus Prime collectibles.",
        year="1984",
        brand="Hasbro",
        series="Transformers Generation 1",
        playerOrCharacter="Optimus Prime",
        rarity="Vintage Figure",
        estimatedGrade="Loose Good",
        edition="G1",
        country="Japan",
        material="Plastic and die-cast metal",
        notes="Original trailer, fists, blaster, and stickers materially affect value.",
    ),
    RecognitionResult(
        title="Magic: The Gathering Black Lotus Reprint",
        category="Trading Card",
        confidence=82,
        estimatedValue=75,
        condition="Lightly Played",
        recommendation="Verify set symbol and authenticity before listing.",
        description="Fantasy trading card layout resembles a premium MTG collectible.",
        detectedObjects=["Card", "Trading Card", "Fantasy", "Lotus"],
        aiProvider="mock",
        processingTimeMs=128,
        primaryMatch="Magic: The Gathering Black Lotus Reprint",
        alternativeMatches=_alternatives(
            (
                "Magic: The Gathering Lotus Petal",
                "Trading Card",
                71,
                "Lotus-themed artwork and MTG layout overlap.",
            ),
            (
                "Magic: The Gathering Mox Pearl",
                "Trading Card",
                61,
                "Vintage artifact card styling is similar.",
            ),
            (
                "Magic: The Gathering Collector's Edition Black Lotus",
                "Trading Card",
                58,
                "Same named card family but edition markers need verification.",
            ),
        ),
        confidenceExplanation="Moderate confidence from fantasy card layout and lotus/artifact visual cues.",
        detectionQuality="Fair - text and set symbol need a sharper close-up for authentication.",
        aiReasoning="The card resembles MTG styling and lotus-themed artwork, but edition and authenticity require manual confirmation.",
        year="1993",
        brand="Magic: The Gathering",
        setName="Collector's Edition",
        series="MTG",
        cardNumber="Black Lotus",
        playerOrCharacter="Black Lotus",
        rarity="Rare",
        estimatedGrade="Lightly Played",
        language="English",
        edition="Collector's Edition/Reprint",
        country="United States",
        material="Cardstock",
        notes="Check corners, back border, and set markings to distinguish reprint editions.",
    ),
]


class MockRecognitionProvider(AIRecognitionProvider):
    _last_index: int | None = None

    def recognize(self, image_path: Path) -> RecognitionResult:
        if len(MOCK_COLLECTIBLES) == 1:
            return MOCK_COLLECTIBLES[0]

        available_indexes = [
            index
            for index in range(len(MOCK_COLLECTIBLES))
            if index != self.__class__._last_index
        ]
        selected_index = random.choice(available_indexes)
        self.__class__._last_index = selected_index

        return MOCK_COLLECTIBLES[selected_index]

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        return self.recognize(Path(image_payload.get("localFilePath") or "uploads/mock.jpg"))


# Backwards-compatible alias for existing tests/imports.
MockRecognitionService = MockRecognitionProvider

# Product-facing provider alias used by the real AI integration roadmap.
MockAiProvider = MockRecognitionProvider
