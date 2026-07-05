import base64
import binascii
from dataclasses import dataclass
from pathlib import Path

from app.core.config import ALLOWED_CONTENT_TYPES, ALLOWED_EXTENSIONS, MAX_IMAGE_BYTES
from app.services.analyzer.errors import AnalyzerPipelineError


@dataclass(frozen=True)
class NormalizedImageMetadata:
    file_name: str
    mime_type: str
    size_bytes: int
    image_source: str
    local_file_path: str
    base64_image: str | None = None
    base64_preview: str | None = None

    def to_api_payload(self) -> dict:
        return {
            "fileName": self.file_name,
            "mimeType": self.mime_type,
            "sizeBytes": self.size_bytes,
            "imageSource": self.image_source,
            "localFilePath": self.local_file_path,
            **({"base64Image": self.base64_image} if self.base64_image else {}),
            **({"base64Preview": self.base64_preview} if self.base64_preview else {}),
        }


class AnalyzerImageValidator:
    def validate_metadata(self, image_payload: dict) -> NormalizedImageMetadata:
        file_name = str(image_payload.get("fileName") or "").strip()
        mime_type = str(image_payload.get("mimeType") or "").strip().lower()
        size_bytes = _parse_int(image_payload.get("sizeBytes"))
        image_source = str(image_payload.get("imageSource") or "unknown").strip() or "unknown"
        local_file_path = str(image_payload.get("localFilePath") or "").strip()
        base64_image = _optional_string(image_payload.get("base64Image"))
        base64_preview = _optional_string(image_payload.get("base64Preview"))

        self._validate_type(file_name=file_name, mime_type=mime_type)
        self._validate_size(size_bytes)

        if base64_image:
            image_bytes = self._decode_base64_image(base64_image)
            self.validate_bytes(
                image_bytes,
                file_name=file_name,
                mime_type=mime_type,
                declared_size=size_bytes,
            )

        return NormalizedImageMetadata(
            file_name=file_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            image_source=image_source,
            local_file_path=local_file_path,
            base64_image=base64_image,
            base64_preview=base64_preview,
        )

    def validate_bytes(
        self,
        image_bytes: bytes,
        *,
        file_name: str,
        mime_type: str,
        declared_size: int | None = None,
    ) -> None:
        self._validate_type(file_name=file_name, mime_type=mime_type)
        actual_size = len(image_bytes)
        self._validate_size(actual_size)
        if declared_size is not None and declared_size > MAX_IMAGE_BYTES:
            self._raise_image_too_large(declared_size)
        if actual_size <= 0:
            raise AnalyzerPipelineError(
                code="invalid_image",
                message="Image file is empty or unreadable.",
                status_code=422,
            )
        detected_mime = _detect_mime_type(image_bytes)
        if detected_mime is None:
            raise AnalyzerPipelineError(
                code="invalid_image",
                message="Image file is corrupt or unreadable.",
                status_code=422,
            )
        if detected_mime != mime_type:
            raise AnalyzerPipelineError(
                code="unsupported_media_type",
                message="Image content does not match the declared media type.",
                status_code=415,
                details={"detectedMimeType": detected_mime, "declaredMimeType": mime_type},
            )

    def _validate_type(self, *, file_name: str, mime_type: str) -> None:
        extension = Path(file_name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS or mime_type not in ALLOWED_CONTENT_TYPES:
            raise AnalyzerPipelineError(
                code="unsupported_media_type",
                message="Unsupported image type. Upload a jpg, jpeg, or png image.",
                status_code=415,
                details={
                    "supportedExtensions": sorted(ALLOWED_EXTENSIONS),
                    "supportedMimeTypes": sorted(ALLOWED_CONTENT_TYPES),
                },
            )

    def _validate_size(self, size_bytes: int) -> None:
        if size_bytes <= 0:
            raise AnalyzerPipelineError(
                code="invalid_image",
                message="Image size must be greater than zero bytes.",
                status_code=422,
            )
        if size_bytes > MAX_IMAGE_BYTES:
            self._raise_image_too_large(size_bytes)

    def _decode_base64_image(self, value: str) -> bytes:
        encoded = value.split(",", 1)[1] if value.startswith("data:") else value
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AnalyzerPipelineError(
                code="invalid_image",
                message="Image base64 payload is invalid.",
                status_code=422,
            ) from exc

    def _raise_image_too_large(self, size_bytes: int) -> None:
        raise AnalyzerPipelineError(
            code="image_too_large",
            message="Image is too large. Maximum upload size is 10MB.",
            status_code=413,
            details={"maxImageBytes": MAX_IMAGE_BYTES, "sizeBytes": size_bytes},
        )


def _detect_mime_type(image_bytes: bytes) -> str | None:
    if image_bytes.startswith(b"\xff\xd8\xff") and image_bytes.endswith(b"\xff\xd9"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and image_bytes[12:16] == b"IHDR":
        return "image/png"
    return None


def _parse_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_string(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
