import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.services.ai.base_recognition_service import (
    AIRecognitionProvider,
    AlternativeMatch,
    RecognitionResult,
)
from app.services.ai.openai_recognition_provider import AIProviderNotConfiguredError

logger = logging.getLogger("collectiq.ai.gemini")


class GeminiProviderError(RuntimeError):
    """Raised when Gemini recognition cannot complete."""


class GeminiInvalidResponseError(GeminiProviderError):
    """Raised when Gemini returns output that cannot be parsed."""


class GeminiTimeoutError(GeminiProviderError):
    """Raised when the Gemini request times out."""


class GeminiRecognitionProvider(AIRecognitionProvider):
    provider_name = "gemini"
    api_base_url = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = settings.gemini_api_key if api_key is None else api_key
        self._model = model or settings.gemini_model
        self._timeout_seconds = (
            settings.gemini_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        self._client = client or httpx.Client(timeout=self._timeout_seconds)

    def recognize(self, image_path: Path) -> RecognitionResult:
        if not self._api_key.strip():
            raise AIProviderNotConfiguredError(
                "GEMINI_API_KEY is required when AI_PROVIDER=gemini."
            )

        started_at = time.perf_counter()
        media_type = self._media_type_for(image_path)
        image_bytes = image_path.read_bytes()
        payload = self._build_payload(
            image_base64=base64.b64encode(image_bytes).decode("ascii"),
            mime_type=media_type,
            prompt_context={
                "imageSource": "uploaded file",
                "fileName": image_path.name,
                "mimeType": media_type,
            },
        )
        return self._recognize_with_payload(payload, started_at)

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        if not self._api_key.strip():
            raise AIProviderNotConfiguredError(
                "GEMINI_API_KEY is required when AI_PROVIDER=gemini."
            )

        started_at = time.perf_counter()
        image_base64 = self._base64_from_api_payload(image_payload)
        mime_type = str(image_payload.get("mimeType") or "application/octet-stream")
        payload = self._build_payload(
            image_base64=image_base64,
            mime_type=mime_type,
            prompt_context={
                "imageSource": request_metadata.get("imageSource")
                or image_payload.get("imageSource")
                or "unknown",
                "requestedCategory": request_metadata.get("requestedCategory") or "none",
                "fileName": image_payload.get("fileName") or "unknown",
                "mimeType": mime_type,
                "appVersion": request_metadata.get("appVersion") or "unknown",
            },
        )
        return self._recognize_with_payload(payload, started_at)

    def _recognize_with_payload(
        self,
        payload: dict[str, Any],
        started_at: float,
    ) -> RecognitionResult:
        try:
            response = self._client.post(
                self._endpoint_url(),
                json=payload,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise GeminiTimeoutError("Gemini recognition request timed out.") from exc
        except httpx.HTTPError as exc:
            raise GeminiProviderError(
                f"Gemini recognition request failed: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise GeminiProviderError(
                "Gemini recognition request failed with "
                f"status {response.status_code}: {response.text}"
            )

        try:
            response_body = response.json()
        except ValueError as exc:
            raise GeminiInvalidResponseError(
                "Gemini response body was not valid JSON."
            ) from exc

        output_text = self._extract_output_text(response_body)
        result_payload = self._parse_json_object(output_text)
        processing_time_ms = int((time.perf_counter() - started_at) * 1000)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Gemini recognition provider=%s model=%s latencyMs=%s "
                "processingTimeMs=%s",
                self.provider_name,
                self._model,
                processing_time_ms,
                processing_time_ms,
            )
        return self._to_recognition_result(result_payload, processing_time_ms)

    def _endpoint_url(self) -> str:
        return f"{self.api_base_url}/models/{self._model}:generateContent?key={self._api_key}"

    def _build_payload(
        self,
        *,
        image_base64: str,
        mime_type: str,
        prompt_context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": self._prompt_text(prompt_context)},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": image_base64,
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "response_mime_type": "application/json",
            },
        }

    def _prompt_text(self, context: dict[str, Any]) -> str:
        return (
            "You are CollectIQ AI, a careful collectible identification and "
            "valuation assistant. Analyze only the provided image. Return JSON "
            "for a collectible recognition result with these keys: title, "
            "category, confidence, estimatedValue, condition, recommendation, "
            "description, detectedObjects, fieldConfidence, confidenceLevel, "
            "lowConfidenceReasons, imageQualityIssues, scanRecommendations, "
            "primaryMatch, alternativeMatches, confidenceExplanation, "
            "detectionQuality, aiReasoning, year, brand, setName, series, "
            "cardNumber, playerOrCharacter, rarity, estimatedGrade, language, "
            "edition, country, mint, material, notes. Use null or Unknown when "
            "not visible. Confidence must be 0-100 and based on image quality, "
            "visible identifiers, and ambiguity. If value is unavailable, use "
            "0 because the existing API contract requires a number. Include "
            "exactly three alternativeMatches with title, category, confidence, "
            "and reason. Do not invent market movement. Context: "
            f"imageSource={context.get('imageSource')}; "
            f"requestedCategory={context.get('requestedCategory')}; "
            f"fileName={context.get('fileName')}; "
            f"mimeType={context.get('mimeType')}; "
            f"appVersion={context.get('appVersion')}."
        )

    def _base64_from_api_payload(self, image_payload: dict) -> str:
        local_path_value = str(image_payload.get("localFilePath") or "").strip()
        local_path = Path(local_path_value)
        if local_path_value and local_path.exists() and local_path.is_file():
            return base64.b64encode(local_path.read_bytes()).decode("ascii")

        encoded_image = image_payload.get("base64Image") or image_payload.get(
            "base64Preview"
        )
        if isinstance(encoded_image, str) and encoded_image.strip():
            encoded = (
                encoded_image.split(",", 1)[1]
                if encoded_image.startswith("data:")
                else encoded_image
            )
            try:
                base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise GeminiProviderError(
                    "Image payload base64 data was invalid."
                ) from exc
            return encoded.strip()

        raise GeminiProviderError(
            "Gemini analysis requires backend-readable image bytes. "
            "Provide a stored file path or base64Image in the backend payload."
        )

    def _extract_output_text(self, response_body: dict[str, Any]) -> str:
        text = response_body.get("text")
        if isinstance(text, str) and text.strip():
            return text

        for candidate in response_body.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            for part in content.get("parts", []):
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    return text

        raise GeminiInvalidResponseError(
            "Gemini response did not include structured output text."
        )

    def _parse_json_object(self, output_text: str) -> dict[str, Any]:
        stripped = output_text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.lower().startswith("json"):
                stripped = stripped[4:].strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise GeminiInvalidResponseError(
                "Gemini structured output was not valid JSON."
            ) from exc

        if not isinstance(parsed, dict):
            raise GeminiInvalidResponseError(
                "Gemini structured output must be a JSON object."
            )
        return parsed

    def _to_recognition_result(
        self,
        payload: dict[str, Any],
        processing_time_ms: int,
    ) -> RecognitionResult:
        title = _string(payload.get("title"), "Unknown collectible")
        category = _string(payload.get("category"), "Other")
        confidence = _confidence(payload.get("confidence"), 40)
        condition = _string(payload.get("condition"), "Unknown")
        recommendation = _string(
            payload.get("recommendation"),
            "Review visible details before saving.",
        )
        description = _string(
            payload.get("description"),
            "Gemini analysis could not verify all collectible details.",
        )
        primary_match = _string(payload.get("primaryMatch"), title)
        detected_objects = _string_list(payload.get("detectedObjects")) or [category]
        alternative_matches = self._parse_alternative_matches(
            payload.get("alternativeMatches"),
            category,
            confidence,
        )
        confidence_explanation = _string(
            payload.get("confidenceExplanation"),
            "Confidence reflects visible identifiers and image quality.",
        )
        detection_quality = _string(
            payload.get("detectionQuality"),
            "Needs review - some details may be unclear.",
        )
        ai_reasoning = _string(
            payload.get("aiReasoning"),
            "Analysis is based on visible collectible cues in the image.",
        )

        return RecognitionResult(
            title=title,
            category=category,
            confidence=confidence,
            estimatedValue=max(0, _int(payload.get("estimatedValue"), 0)),
            condition=condition,
            recommendation=recommendation,
            description=description,
            detectedObjects=detected_objects,
            aiProvider=self.provider_name,
            processingTimeMs=max(1, processing_time_ms),
            primaryMatch=primary_match,
            alternativeMatches=alternative_matches,
            confidenceExplanation=confidence_explanation,
            detectionQuality=detection_quality,
            aiReasoning=ai_reasoning,
            year=_optional_string(payload.get("year")),
            brand=_optional_string(payload.get("brand")),
            setName=_optional_string(payload.get("setName")),
            series=_optional_string(payload.get("series")),
            cardNumber=_optional_string(payload.get("cardNumber")),
            playerOrCharacter=_optional_string(payload.get("playerOrCharacter")),
            rarity=_optional_string(payload.get("rarity")),
            estimatedGrade=_optional_string(payload.get("estimatedGrade")),
            language=_optional_string(payload.get("language")),
            edition=_optional_string(payload.get("edition")),
            country=_optional_string(payload.get("country")),
            mint=_optional_string(payload.get("mint")),
            material=_optional_string(payload.get("material")),
            notes=_optional_string(payload.get("notes")),
            fieldConfidence=self._parse_field_confidence(
                payload.get("fieldConfidence"),
                confidence,
            ),
            confidenceLevel=_confidence_level(payload.get("confidenceLevel"), confidence),
            lowConfidenceReasons=_string_list(payload.get("lowConfidenceReasons")),
            imageQualityIssues=_string_list(payload.get("imageQualityIssues")),
            scanRecommendations=_string_list(payload.get("scanRecommendations")),
        )

    def _parse_alternative_matches(
        self,
        raw_matches: Any,
        category: str,
        confidence: int,
    ) -> list[AlternativeMatch]:
        matches: list[AlternativeMatch] = []
        if isinstance(raw_matches, list):
            for raw_match in raw_matches[:3]:
                if not isinstance(raw_match, dict):
                    continue
                matches.append(
                    AlternativeMatch(
                        title=_string(raw_match.get("title"), "Possible collectible"),
                        category=_string(raw_match.get("category"), category),
                        confidence=_confidence(
                            raw_match.get("confidence"),
                            max(0, confidence - 15),
                        ),
                        reason=_string(
                            raw_match.get("reason"),
                            "Similar visible collectible cues.",
                        ),
                    )
                )

        while len(matches) < 3:
            matches.append(
                AlternativeMatch(
                    title="Unknown collectible variant",
                    category=category,
                    confidence=max(0, confidence - 20 - (len(matches) * 5)),
                    reason="Not enough visible detail to identify a closer match.",
                )
            )
        return matches

    def _parse_field_confidence(self, value: Any, confidence: int) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        parsed: dict[str, int] = {}
        for key, raw_value in value.items():
            if not isinstance(key, str) or not key.strip():
                continue
            parsed[key.strip()] = _confidence(raw_value, confidence)
        return parsed

    def _media_type_for(self, image_path: Path) -> str:
        extension = image_path.suffix.lower()
        if extension in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if extension == ".png":
            return "image/png"
        return "application/octet-stream"


def _string(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized.lower() in {"unknown", "null", "none"}:
        return None
    return normalized


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _confidence(value: Any, fallback: int) -> int:
    return max(0, min(100, _int(value, fallback)))


def _confidence_level(value: Any, confidence: int) -> str:
    if isinstance(value, str) and value in {"High", "Medium", "Low"}:
        return value
    if confidence >= 90:
        return "High"
    if confidence >= 70:
        return "Medium"
    return "Low"


GeminiVisionProvider = GeminiRecognitionProvider
