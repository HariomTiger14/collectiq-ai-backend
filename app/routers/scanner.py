"""Scanner analysis routes."""

from fastapi import APIRouter, Depends, File, UploadFile

from app.core.dependencies import get_ai_service, get_image_service
from app.schemas.scan_response import ScanResponse
from app.services.ai_service import AIService
from app.services.image_service import ImageService

router = APIRouter(prefix="/scanner", tags=["scanner"])


@router.post("/analyze", response_model=ScanResponse)
async def analyze_collectible(
    image: UploadFile = File(
        ...,
        description="Collectible image upload. Supported formats: jpg, jpeg, png. Max size: 10MB.",
    ),
    ai_service: AIService = Depends(get_ai_service),
    image_service: ImageService = Depends(get_image_service),
) -> ScanResponse:
    """Upload and analyze a collectible image with mocked recognition data."""
    uploaded_image = await image_service.save_upload(image)
    return ai_service.analyze_collectible(uploaded_image)
