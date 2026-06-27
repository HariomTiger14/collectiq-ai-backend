"""Dependency providers for FastAPI routes."""

from functools import lru_cache

from app.services.ai_service import AIService
from app.services.image_service import ImageService
from app.services.portfolio_service import PortfolioService


@lru_cache
def get_ai_service() -> AIService:
    """Return the shared AI service instance."""
    return AIService()


@lru_cache
def get_image_service() -> ImageService:
    """Return the shared image service instance."""
    return ImageService()


@lru_cache
def get_portfolio_service() -> PortfolioService:
    """Return the shared in-memory portfolio service instance."""
    return PortfolioService()
