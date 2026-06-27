"""Scanner response schemas."""

from pydantic import BaseModel, Field


class ScanResponse(BaseModel):
    """Mocked collectible analysis response."""

    success: bool = Field(description="Whether analysis completed successfully.")
    filename: str = Field(description="Saved upload filename.")
    image_url: str = Field(
        alias="imageUrl",
        description="Public URL path for the saved image.",
    )
    title: str = Field(description="Recognized collectible title.")
    category: str = Field(description="Recognized collectible category.")
    confidence: int = Field(description="Recognition confidence percentage.")
    estimated_value: int = Field(
        alias="estimatedValue",
        description="Estimated market value.",
    )
    condition: str = Field(description="Estimated item condition.")
    recommendation: str = Field(description="Recommended next action.")

    model_config = {"populate_by_name": True}
