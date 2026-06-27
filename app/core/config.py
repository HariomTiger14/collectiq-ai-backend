"""Application configuration for CollectIQ AI."""

from dataclasses import dataclass
from os import getenv
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the backend application."""

    app_name: str = "CollectIQ AI Backend"
    version: str = "1.0"
    environment: str = "development"
    upload_dir: Path = Path("uploads")
    max_upload_size_bytes: int = 10 * 1024 * 1024


settings = Settings(
    environment=getenv("APP_ENV", "development"),
)
