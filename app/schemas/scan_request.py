"""Scanner request schemas."""

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    """Request body for collectible image analysis."""

    image_url: str | None = Field(
        default=None,
        description="Optional image URL for future remote analysis.",
    )
    image_base64: str | None = Field(
        default=None,
        description="Optional base64 image payload for future uploads.",
    )
