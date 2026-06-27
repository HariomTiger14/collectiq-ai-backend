"""Image validation and upload persistence service."""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile, status

from app.core.config import settings


@dataclass(frozen=True)
class UploadedImage:
    """Metadata for a successfully saved uploaded image."""

    filename: str
    image_url: str
    path: Path


class ImageService:
    """Service responsible for image validation and local persistence."""

    _allowed_extensions = {".jpg", ".jpeg", ".png"}
    _allowed_content_types = {"image/jpeg", "image/png"}
    _chunk_size_bytes = 1024 * 1024

    def __init__(
        self,
        upload_dir: Path = settings.upload_dir,
        max_upload_size_bytes: int = settings.max_upload_size_bytes,
    ) -> None:
        """Create an image service and ensure the upload directory exists."""
        self._upload_dir = upload_dir
        self._max_upload_size_bytes = max_upload_size_bytes
        self._upload_dir.mkdir(parents=True, exist_ok=True)

    async def save_upload(self, image: UploadFile) -> UploadedImage:
        """Validate and save an uploaded image using a UUID filename."""
        extension = self._validate_upload_metadata(image)
        filename = f"{uuid4()}{extension}"
        destination = self._upload_dir / filename

        try:
            await self._save_with_size_limit(image, destination)
        except HTTPException:
            destination.unlink(missing_ok=True)
            raise
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unable to save uploaded image.",
            ) from error
        finally:
            await image.close()

        return UploadedImage(
            filename=filename,
            image_url=f"/uploads/{filename}",
            path=destination,
        )

    def _validate_upload_metadata(self, image: UploadFile) -> str:
        """Validate upload filename extension and content type."""
        if image.filename is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Image filename is required.",
            )

        extension = Path(image.filename).suffix.lower()
        if extension not in self._allowed_extensions:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported image type. Use jpg, jpeg, or png.",
            )

        if image.content_type not in self._allowed_content_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported image content type.",
            )

        return extension

    async def _save_with_size_limit(
        self,
        image: UploadFile,
        destination: Path,
    ) -> None:
        """Persist the image while enforcing the configured max upload size."""
        total_size = 0

        with destination.open("wb") as file:
            while chunk := await image.read(self._chunk_size_bytes):
                total_size += len(chunk)
                if total_size > self._max_upload_size_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Image exceeds the 10MB size limit.",
                    )
                file.write(chunk)

        if total_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded image is empty.",
            )
