"""AI recognition service."""

from app.schemas.scan_response import ScanResponse
from app.services.image_service import UploadedImage


class AIService:
    """Service responsible for collectible recognition workflows."""

    def analyze_collectible(self, image: UploadedImage) -> ScanResponse:
        """Return mocked collectible recognition data."""
        return ScanResponse(
            success=True,
            filename=image.filename,
            image_url=image.image_url,
            title="1999 Pokémon Charizard",
            category="Trading Card",
            confidence=94,
            estimated_value=1850,
            condition="Near Mint",
            recommendation="Consider grading before selling.",
        )
