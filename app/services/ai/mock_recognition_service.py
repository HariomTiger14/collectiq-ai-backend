import hashlib
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


def _mock(
    *,
    title: str,
    category: str,
    year: str | None,
    brand: str,
    condition: str,
    value: int,
    confidence: int,
    series: str | None = None,
    set_name: str | None = None,
    card_number: str | None = None,
    character: str | None = None,
    rarity: str | None = None,
    grade: str | None = None,
    country: str | None = None,
    mint: str | None = None,
    material: str | None = None,
    notes: str = "SIT MOCK DATA - verify before sale or grading.",
) -> RecognitionResult:
    detected = [category, brand, *(filter(None, [character, series, rarity]))]
    return RecognitionResult(
        title=title,
        category=category,
        confidence=confidence,
        estimatedValue=value,
        condition=condition,
        recommendation=_recommendation_for(category),
        description=(
            f"Realistic SIT mock match for {title}. This result is generated "
            "for demo and scanner validation only."
        ),
        detectedObjects=detected,
        aiProvider="mock",
        processingTimeMs=120 + (len(title) % 40),
        primaryMatch=title,
        alternativeMatches=_alternatives(
            (
                f"{title} variant",
                category,
                max(confidence - 12, 45),
                "Same collectible family with variant details to verify.",
            ),
            (
                f"{brand} related collectible",
                category,
                max(confidence - 20, 38),
                "Brand and era overlap, but exact identifiers may differ.",
            ),
            (
                f"Similar era {category}",
                category,
                max(confidence - 28, 30),
                "Comparable category and period; needs manual confirmation.",
            ),
        ),
        confidenceExplanation=(
            "Mock confidence is based on visible collectible type, era, brand, "
            "and condition cues."
        ),
        detectionQuality=(
            "Good - mock SIT analysis assumes the main collectible is visible "
            "enough for review."
        ),
        aiReasoning=(
            "SIT mock result selected from a varied collectible pool using the "
            "uploaded image signal."
        ),
        year=year,
        brand=brand,
        setName=set_name,
        series=series,
        cardNumber=card_number,
        playerOrCharacter=character,
        rarity=rarity,
        estimatedGrade=grade,
        language="English",
        country=country,
        mint=mint,
        material=material,
        notes=notes,
    )


def _recommendation_for(category: str) -> str:
    normalized = category.lower()
    if "card" in normalized:
        return "Sleeve it, verify authenticity, and inspect centering before grading."
    if normalized in {"coin", "stamp"}:
        return "Avoid cleaning and confirm authenticity with close-up inspection."
    if "comic" in normalized:
        return "Bag and board it, then inspect spine, staples, and page color."
    return "Document condition and accessories before saving or listing."


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
    _mock(
        title="1999 Pokemon Blastoise Holo",
        category="Pokemon Card",
        year="1999",
        brand="Pokemon",
        condition="Excellent",
        value=620,
        confidence=88,
        series="Pokemon TCG",
        set_name="Base Set",
        card_number="2/102",
        character="Blastoise",
        rarity="Holo Rare",
        grade="PSA 7-8",
        material="Cardstock",
        notes="SIT MOCK DATA - check holo scratching and edge whitening.",
    ),
    _mock(
        title="2000 Pokemon Lugia Neo Genesis Holo",
        category="Pokemon Card",
        year="2000",
        brand="Pokemon",
        condition="Lightly Played",
        value=410,
        confidence=84,
        series="Pokemon TCG",
        set_name="Neo Genesis",
        card_number="9/111",
        character="Lugia",
        rarity="Holo Rare",
        grade="PSA 6-7",
        material="Cardstock",
    ),
    _mock(
        title="1989 Upper Deck Ken Griffey Jr. Rookie",
        category="Sports Card",
        year="1989",
        brand="Upper Deck",
        condition="Near Mint",
        value=140,
        confidence=87,
        series="MLB",
        set_name="Upper Deck Baseball",
        card_number="1",
        character="Ken Griffey Jr.",
        rarity="Rookie Card",
        grade="PSA 8-9",
        material="Cardstock",
    ),
    _mock(
        title="1993 SP Derek Jeter Foil Rookie",
        category="Sports Card",
        year="1993",
        brand="SP",
        condition="Excellent",
        value=520,
        confidence=82,
        series="MLB",
        set_name="SP Baseball",
        card_number="279",
        character="Derek Jeter",
        rarity="Foil Rookie",
        grade="PSA 7",
        material="Foil cardstock",
    ),
    _mock(
        title="1964 Kennedy Half Dollar",
        category="Coin",
        year="1964",
        brand="United States Mint",
        condition="About Uncirculated",
        value=24,
        confidence=83,
        series="Kennedy Half Dollar",
        country="United States",
        mint="Denver",
        material="90% silver",
    ),
    _mock(
        title="1911 Australian Half Sovereign",
        category="Coin",
        year="1911",
        brand="Royal Mint",
        condition="Fine",
        value=385,
        confidence=78,
        series="Half Sovereign",
        country="Australia",
        mint="Sydney",
        material="Gold",
    ),
    _mock(
        title="Star Wars 1978 Darth Vader 12-Back",
        category="Action Figure",
        year="1978",
        brand="Kenner",
        condition="Loose Complete",
        value=310,
        confidence=82,
        series="Star Wars",
        character="Darth Vader",
        rarity="12-Back",
        grade="Loose Complete",
        country="United States",
        material="Plastic",
    ),
    _mock(
        title="GI Joe Snake Eyes V2 Figure",
        category="Action Figure",
        year="1985",
        brand="Hasbro",
        condition="Very Good",
        value=120,
        confidence=80,
        series="GI Joe: A Real American Hero",
        character="Snake Eyes",
        rarity="Vintage Figure",
        grade="Loose VG",
        material="Plastic",
    ),
    _mock(
        title="Uncanny X-Men #266",
        category="Comic Book",
        year="1990",
        brand="Marvel Comics",
        condition="Very Fine",
        value=180,
        confidence=86,
        series="Uncanny X-Men",
        card_number="#266",
        character="Gambit",
        rarity="Key Issue",
        grade="VF",
        material="Paper",
    ),
    _mock(
        title="Batman: The Killing Joke First Print",
        category="Comic Book",
        year="1988",
        brand="DC Comics",
        condition="Near Mint",
        value=110,
        confidence=84,
        series="Batman",
        character="Batman / Joker",
        rarity="First Print",
        grade="NM-",
        material="Paper",
    ),
    _mock(
        title="Penny Black Stamp",
        category="Stamp",
        year="1840",
        brand="Royal Mail",
        condition="Used",
        value=190,
        confidence=77,
        series="Victorian Definitive",
        rarity="Classic Stamp",
        grade="Used Fine",
        country="United Kingdom",
        material="Gummed paper",
    ),
    _mock(
        title="1918 Inverted Jenny Stamp Reproduction",
        category="Stamp",
        year="1918",
        brand="US Post Office",
        condition="Mint Hinged",
        value=35,
        confidence=73,
        series="Airmail",
        rarity="Reproduction",
        grade="MH",
        country="United States",
        material="Gummed paper",
    ),
    _mock(
        title="Pokemon Red Game Boy Cartridge",
        category="Retro Game",
        year="1998",
        brand="Nintendo",
        condition="Good",
        value=85,
        confidence=85,
        series="Game Boy",
        character="Pokemon",
        rarity="Loose Cartridge",
        material="Plastic cartridge",
    ),
    _mock(
        title="The Legend of Zelda NES Gold Cartridge",
        category="Retro Game",
        year="1987",
        brand="Nintendo",
        condition="Very Good",
        value=120,
        confidence=86,
        series="NES",
        character="Link",
        rarity="Gold Cartridge",
        material="Plastic cartridge",
    ),
    _mock(
        title="Super Mario 64 Nintendo 64 Cart",
        category="Retro Game",
        year="1996",
        brand="Nintendo",
        condition="Good",
        value=65,
        confidence=82,
        series="Nintendo 64",
        character="Mario",
        rarity="Launch Title",
        material="Plastic cartridge",
    ),
    _mock(
        title="Yu-Gi-Oh! Blue-Eyes White Dragon SDK-001",
        category="Trading Card",
        year="2002",
        brand="Konami",
        condition="Near Mint",
        value=210,
        confidence=84,
        series="Yu-Gi-Oh!",
        set_name="Starter Deck Kaiba",
        card_number="SDK-001",
        character="Blue-Eyes White Dragon",
        rarity="Ultra Rare",
        grade="Raw NM",
        material="Cardstock",
    ),
    _mock(
        title="Lorcana Elsa Spirit of Winter Enchanted",
        category="Trading Card",
        year="2023",
        brand="Ravensburger",
        condition="Near Mint",
        value=760,
        confidence=80,
        series="Disney Lorcana",
        set_name="The First Chapter",
        character="Elsa",
        rarity="Enchanted",
        grade="Raw NM",
        material="Cardstock",
    ),
    _mock(
        title="1990 Marvel Universe Series 1 Wolverine",
        category="Trading Card",
        year="1990",
        brand="Impel",
        condition="Near Mint",
        value=38,
        confidence=83,
        series="Marvel Cards",
        set_name="Marvel Universe Series 1",
        card_number="23",
        character="Wolverine",
        rarity="Base Card",
        material="Cardstock",
    ),
    _mock(
        title="1983 Cabbage Patch Kids Doll",
        category="Vintage Toy",
        year="1983",
        brand="Coleco",
        condition="Good",
        value=95,
        confidence=78,
        series="Cabbage Patch Kids",
        rarity="First Wave",
        material="Fabric and vinyl",
    ),
    _mock(
        title="1982 Masters of the Universe He-Man Figure",
        category="Vintage Toy",
        year="1982",
        brand="Mattel",
        condition="Loose Good",
        value=70,
        confidence=81,
        series="Masters of the Universe",
        character="He-Man",
        rarity="Vintage Figure",
        material="Plastic",
    ),
    _mock(
        title="Hot Wheels Redline Custom Camaro",
        category="Vintage Toy",
        year="1968",
        brand="Mattel",
        condition="Play Worn",
        value=180,
        confidence=76,
        series="Hot Wheels Redline",
        rarity="Redline",
        material="Die-cast metal",
    ),
    _mock(
        title="Teenage Mutant Ninja Turtles Leonardo Figure",
        category="Vintage Toy",
        year="1988",
        brand="Playmates",
        condition="Very Good",
        value=85,
        confidence=79,
        series="TMNT",
        character="Leonardo",
        rarity="Soft Head Variant",
        material="Plastic",
    ),
    _mock(
        title="1992 SkyBox Dream Team Michael Jordan",
        category="Sports Card",
        year="1992",
        brand="SkyBox",
        condition="Near Mint",
        value=55,
        confidence=84,
        series="Olympics",
        set_name="USA Basketball",
        card_number="USA11",
        character="Michael Jordan",
        rarity="Dream Team",
        material="Cardstock",
    ),
    _mock(
        title="1963 Topps Pete Rose Rookie",
        category="Sports Card",
        year="1963",
        brand="Topps",
        condition="Good",
        value=950,
        confidence=77,
        series="MLB",
        set_name="Topps Baseball",
        card_number="537",
        character="Pete Rose",
        rarity="Rookie Card",
        grade="PSA 3-4",
        material="Cardstock",
    ),
]


class MockRecognitionProvider(AIRecognitionProvider):
    def recognize(self, image_path: Path) -> RecognitionResult:
        return MOCK_COLLECTIBLES[_index_for_path(image_path)]

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        seed = (
            image_payload.get("base64Image")
            or image_payload.get("base64Preview")
            or image_payload.get("localFilePath")
            or image_payload.get("fileName")
            or request_metadata.get("imagePath")
            or request_metadata.get("timestamp")
            or "uploads/mock.jpg"
        )
        requested_category = str(request_metadata.get("requestedCategory") or "").strip()
        if requested_category:
            matches = [
                item
                for item in MOCK_COLLECTIBLES
                if _matches_requested_category(item.category, requested_category)
            ]
            if matches:
                index = _hash_to_index(str(seed), len(matches))
                return matches[index]

        return self.recognize(Path(str(seed)))


def _index_for_path(image_path: Path) -> int:
    seed = str(image_path)
    if Path(seed).name == "card.png":
        return 0

    return _hash_to_index(seed, len(MOCK_COLLECTIBLES))


def _hash_to_index(seed: str, pool_size: int) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % pool_size


def _matches_requested_category(category: str, requested_category: str) -> bool:
    category = category.lower()
    requested = requested_category.lower()
    if requested in category or category in requested:
        return True
    if requested in {"toy", "toys", "figure", "action figure"}:
        return category in {"toy/figure", "action figure", "vintage toy"}
    if requested in {"comic", "comics", "comic book", "comic books"}:
        return category in {"comic", "comic book"}
    if requested in {"game", "games", "video game", "retro game"}:
        return category == "retro game"
    if requested in {"card", "cards", "trading card"}:
        return "card" in category
    return False


# Backwards-compatible alias for existing tests/imports.
MockRecognitionService = MockRecognitionProvider

# Product-facing provider alias used by the real AI integration roadmap.
MockAiProvider = MockRecognitionProvider
