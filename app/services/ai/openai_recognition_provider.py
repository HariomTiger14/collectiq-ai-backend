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

logger = logging.getLogger("collectiq.ai.openai")


class AIProviderNotConfiguredError(RuntimeError):
    """Raised when a selected AI provider is not ready for recognition."""


class OpenAIProviderError(RuntimeError):
    """Raised when OpenAI recognition cannot complete."""


class OpenAIInvalidResponseError(OpenAIProviderError):
    """Raised when OpenAI returns output that cannot be parsed."""


class OpenAITimeoutError(OpenAIProviderError):
    """Raised when the OpenAI request times out."""


class OpenAIRecognitionProvider(AIRecognitionProvider):
    provider_name = "openai"
    responses_url = "https://api.openai.com/v1/responses"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = settings.openai_api_key if api_key is None else api_key
        self._model = model or settings.openai_model
        self._timeout_seconds = (
            settings.openai_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        self._client = client or httpx.Client(timeout=self._timeout_seconds)

    def recognize(self, image_path: Path) -> RecognitionResult:
        if not self._api_key.strip():
            raise AIProviderNotConfiguredError(
                "OPENAI_API_KEY is required when AI_PROVIDER=openai."
            )

        started_at = time.perf_counter()
        payload = self._build_payload(image_path)
        return self._recognize_with_payload(payload, started_at)

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        if not self._api_key.strip():
            raise AIProviderNotConfiguredError(
                "OPENAI_API_KEY is required when AI_PROVIDER=openai."
            )

        started_at = time.perf_counter()
        payload = self._build_payload_from_api_payload(
            request_metadata=request_metadata,
            image_payload=image_payload,
        )
        return self._recognize_with_payload(payload, started_at)

    def _recognize_with_payload(
        self,
        payload: dict[str, Any],
        started_at: float,
    ) -> RecognitionResult:
        prompt_text = self._prompt_from_payload(payload)
        prompt_token_estimate = _estimate_tokens(prompt_text)

        try:
            response = self._client.post(
                self.responses_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise OpenAITimeoutError("OpenAI recognition request timed out.") from exc
        except httpx.HTTPError as exc:
            raise OpenAIProviderError(
                f"OpenAI recognition request failed: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise OpenAIProviderError(
                "OpenAI recognition request failed with "
                f"status {response.status_code}: {response.text}"
            )

        try:
            response_body = response.json()
        except ValueError as exc:
            raise OpenAIInvalidResponseError(
                "OpenAI response body was not valid JSON."
            ) from exc

        output_text = self._extract_structured_output_text(response_body)
        completion_token_estimate = _estimate_tokens(output_text)
        result_payload = self._parse_json_object(output_text)
        processing_time_ms = int((time.perf_counter() - started_at) * 1000)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "OpenAI recognition provider=%s model=%s latencyMs=%s "
                "promptTokensEstimate=%s completionTokensEstimate=%s "
                "processingTimeMs=%s",
                self.provider_name,
                self._model,
                processing_time_ms,
                prompt_token_estimate,
                completion_token_estimate,
                processing_time_ms,
            )
        return self._to_recognition_result(result_payload, processing_time_ms)

    def _build_payload(self, image_path: Path) -> dict[str, Any]:
        encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
        media_type = self._media_type_for(image_path)
        return self._build_payload_for_data_url(
            image_data_url=f"data:{media_type};base64,{encoded_image}",
            prompt_context={
                "imageSource": "uploaded file",
                "fileName": image_path.name,
                "mimeType": media_type,
            },
        )

    def _build_payload_from_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> dict[str, Any]:
        image_data_url = self._image_data_url_from_api_payload(image_payload)
        prompt_context = {
            "imageSource": request_metadata.get("imageSource")
            or image_payload.get("imageSource")
            or "unknown",
            "requestedCategory": request_metadata.get("requestedCategory") or "none",
            "fileName": image_payload.get("fileName") or "unknown",
            "mimeType": image_payload.get("mimeType") or "application/octet-stream",
            "appVersion": request_metadata.get("appVersion") or "unknown",
        }
        return self._build_payload_for_data_url(
            image_data_url=image_data_url,
            prompt_context=prompt_context,
        )

    def _build_payload_for_data_url(
        self,
        *,
        image_data_url: str,
        prompt_context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "model": self._model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": self._prompt_text(prompt_context),
                        },
                        {
                            "type": "input_image",
                            "image_url": image_data_url,
                        },
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "collectible_recognition",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "title",
                            "category",
                            "confidence",
                            "estimatedValue",
                            "condition",
                            "recommendation",
                            "description",
                            "detectedObjects",
                            "fieldConfidence",
                            "confidenceLevel",
                            "lowConfidenceReasons",
                            "imageQualityIssues",
                            "scanRecommendations",
                            "primaryMatch",
                            "alternativeMatches",
                            "confidenceExplanation",
                            "detectionQuality",
                            "aiReasoning",
                            "year",
                            "brand",
                            "setName",
                            "series",
                            "cardNumber",
                            "playerOrCharacter",
                            "rarity",
                            "estimatedGrade",
                            "language",
                            "edition",
                            "country",
                            "mint",
                            "material",
                            "notes",
                        ],
                        "properties": {
                            "title": {"type": "string"},
                            "category": {"type": "string"},
                            "confidence": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 100,
                            },
                            "estimatedValue": {
                                "type": "integer",
                                "minimum": 0,
                            },
                            "condition": {"type": "string"},
                            "recommendation": {"type": "string"},
                            "description": {"type": "string"},
                            "detectedObjects": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "fieldConfidence": {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 100,
                                },
                            },
                            "confidenceLevel": {
                                "type": "string",
                                "enum": ["High", "Medium", "Low"],
                            },
                            "lowConfidenceReasons": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "imageQualityIssues": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": [
                                        "blurry image",
                                        "glare/reflections",
                                        "cropped edges",
                                        "dark image",
                                        "low resolution",
                                        "multiple collectibles in one photo",
                                        "fine details may be hard to read",
                                        "none",
                                    ],
                                },
                            },
                            "scanRecommendations": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "primaryMatch": {"type": "string"},
                            "alternativeMatches": {
                                "type": "array",
                                "minItems": 3,
                                "maxItems": 3,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "title",
                                        "category",
                                        "confidence",
                                        "reason",
                                    ],
                                    "properties": {
                                        "title": {"type": "string"},
                                        "category": {"type": "string"},
                                        "confidence": {
                                            "type": "integer",
                                            "minimum": 0,
                                            "maximum": 100,
                                        },
                                        "reason": {"type": "string"},
                                    },
                                },
                            },
                            "confidenceExplanation": {"type": "string"},
                            "detectionQuality": {"type": "string"},
                            "aiReasoning": {"type": "string"},
                            "year": {"type": ["string", "null"]},
                            "brand": {"type": ["string", "null"]},
                            "setName": {"type": ["string", "null"]},
                            "series": {"type": ["string", "null"]},
                            "cardNumber": {"type": ["string", "null"]},
                            "playerOrCharacter": {"type": ["string", "null"]},
                            "rarity": {"type": ["string", "null"]},
                            "estimatedGrade": {"type": ["string", "null"]},
                            "language": {"type": ["string", "null"]},
                            "edition": {"type": ["string", "null"]},
                            "country": {"type": ["string", "null"]},
                            "mint": {"type": ["string", "null"]},
                            "material": {"type": ["string", "null"]},
                            "notes": {"type": ["string", "null"]},
                        },
                    },
                }
            },
        }

    def _prompt_text(self, context: dict[str, Any]) -> str:
        return (
            "You are CollectIQ AI, a careful collectible identification and "
            "valuation assistant. Analyze the provided image for a real "
            "collector. Identify the collectible and extract item name, "
            "franchise/brand, category, set or series, visible year, visible "
            "card/issue/coin number, manufacturer or publisher, language, "
            "edition or variant, and raw grading likelihood. Never invent "
            "information: use null or 'Unknown' when a field is not visible. "
            "Return confidence for each extracted field from 0 to 100. Classify "
            "overall confidence as High for >=90, Medium for 70-89, and Low "
            "for <70. If confidence is not High, explain why. Evaluate image "
            "quality for blurry image, glare/reflections, cropped edges, dark "
            "image, low resolution, and multiple collectibles in one photo. "
            "Return actionable scan recommendations. Launch categories are "
            "Pokemon/TCG cards, sports cards, coins, comics, memorabilia, "
            "toys/figures, and other collectibles. Use conservative "
            "Australian-dollar estimates. Prefer uncertainty over overclaiming. "
            "Return strict JSON that matches the provided schema only and "
            "include exactly three plausible alternative matches. Context: "
            f"imageSource={context.get('imageSource')}; "
            f"requestedCategory={context.get('requestedCategory')}; "
            f"fileName={context.get('fileName')}; "
            f"mimeType={context.get('mimeType')}; "
            f"appVersion={context.get('appVersion')}."
        )

    def _image_data_url_from_api_payload(self, image_payload: dict) -> str:
        mime_type = str(image_payload.get("mimeType") or "application/octet-stream")
        local_path = Path(str(image_payload.get("localFilePath") or ""))
        if str(local_path).strip() and local_path.exists():
            encoded_image = base64.b64encode(local_path.read_bytes()).decode("ascii")
            return f"data:{mime_type};base64,{encoded_image}"

        encoded_image = image_payload.get("base64Image") or image_payload.get(
            "base64Preview"
        )
        if isinstance(encoded_image, str) and encoded_image.strip():
            try:
                base64.b64decode(encoded_image, validate=True)
            except ValueError as exc:
                raise OpenAIProviderError(
                    "Image payload base64 data was invalid."
                ) from exc
            return f"data:{mime_type};base64,{encoded_image.strip()}"

        raise OpenAIProviderError(
            "OpenAI analysis requires backend-readable image bytes. "
            "Provide a stored file path or base64Image in the backend payload."
        )

    def _extract_structured_output_text(self, response_body: dict[str, Any]) -> str:
        output_text = response_body.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        for output_item in response_body.get("output", []):
            if not isinstance(output_item, dict):
                continue
            for content_item in output_item.get("content", []):
                if not isinstance(content_item, dict):
                    continue
                text = content_item.get("text")
                if isinstance(text, str) and text.strip():
                    return text

        raise OpenAIInvalidResponseError(
            "OpenAI response did not include structured output text."
        )

    def _parse_json_object(self, output_text: str) -> dict[str, Any]:
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise OpenAIInvalidResponseError(
                "OpenAI structured output was not valid JSON."
            ) from exc

        if not isinstance(parsed, dict):
            raise OpenAIInvalidResponseError(
                "OpenAI structured output must be a JSON object."
            )
        return parsed

    def _to_recognition_result(
        self,
        payload: dict[str, Any],
        processing_time_ms: int,
    ) -> RecognitionResult:
        required_string_fields = [
            "title",
            "category",
            "condition",
            "recommendation",
            "description",
            "primaryMatch",
            "confidenceExplanation",
            "detectionQuality",
            "aiReasoning",
        ]
        for field in required_string_fields:
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                raise OpenAIInvalidResponseError(
                    f"OpenAI structured output missing string field '{field}'."
                )

        detected_objects = payload.get("detectedObjects")
        if not isinstance(detected_objects, list) or not all(
            isinstance(item, str) and item.strip() for item in detected_objects
        ):
            raise OpenAIInvalidResponseError(
                "OpenAI structured output missing detectedObjects list."
            )

        alternative_matches = self._parse_alternative_matches(payload)

        try:
            confidence = int(payload["confidence"])
            estimated_value = int(payload["estimatedValue"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OpenAIInvalidResponseError(
                "OpenAI structured output had invalid numeric fields."
            ) from exc

        return RecognitionResult(
            title=payload["title"].strip(),
            category=payload["category"].strip(),
            confidence=max(0, min(confidence, 100)),
            estimatedValue=max(0, estimated_value),
            condition=payload["condition"].strip(),
            recommendation=payload["recommendation"].strip(),
            description=payload["description"].strip(),
            detectedObjects=[item.strip() for item in detected_objects],
            aiProvider=self.provider_name,
            processingTimeMs=max(1, processing_time_ms),
            primaryMatch=payload["primaryMatch"].strip(),
            alternativeMatches=alternative_matches,
            confidenceExplanation=payload["confidenceExplanation"].strip(),
            detectionQuality=payload["detectionQuality"].strip(),
            aiReasoning=payload["aiReasoning"].strip(),
            year=self._optional_string(payload, "year"),
            brand=self._optional_string(payload, "brand"),
            setName=self._optional_string(payload, "setName"),
            series=self._optional_string(payload, "series"),
            cardNumber=self._optional_string(payload, "cardNumber"),
            playerOrCharacter=self._optional_string(payload, "playerOrCharacter"),
            rarity=self._optional_string(payload, "rarity"),
            estimatedGrade=self._optional_string(payload, "estimatedGrade"),
            language=self._optional_string(payload, "language"),
            edition=self._optional_string(payload, "edition"),
            country=self._optional_string(payload, "country"),
            mint=self._optional_string(payload, "mint"),
            material=self._optional_string(payload, "material"),
            notes=self._optional_string(payload, "notes"),
            fieldConfidence=self._parse_field_confidence(payload),
            confidenceLevel=self._confidence_level(
                payload.get("confidenceLevel"),
                confidence,
            ),
            lowConfidenceReasons=self._parse_string_list(
                payload.get("lowConfidenceReasons")
            ),
            imageQualityIssues=self._parse_string_list(payload.get("imageQualityIssues")),
            scanRecommendations=self._parse_string_list(
                payload.get("scanRecommendations")
            ),
        )

    def _optional_string(self, payload: dict[str, Any], field: str) -> str | None:
        value = payload.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise OpenAIInvalidResponseError(
                f"OpenAI structured output field '{field}' must be a string or null."
            )
        normalized = value.strip()
        return normalized or None

    def _parse_alternative_matches(
        self,
        payload: dict[str, Any],
    ) -> list[AlternativeMatch]:
        raw_matches = payload.get("alternativeMatches")
        if not isinstance(raw_matches, list) or len(raw_matches) != 3:
            raise OpenAIInvalidResponseError(
                "OpenAI structured output must include exactly three alternative matches."
            )

        matches: list[AlternativeMatch] = []
        for raw_match in raw_matches:
            if not isinstance(raw_match, dict):
                raise OpenAIInvalidResponseError(
                    "OpenAI alternative matches must be JSON objects."
                )

            required_fields = ["title", "category", "reason"]
            for field in required_fields:
                value = raw_match.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise OpenAIInvalidResponseError(
                        "OpenAI alternative match missing string field "
                        f"'{field}'."
                    )

            try:
                confidence = int(raw_match["confidence"])
            except (KeyError, TypeError, ValueError) as exc:
                raise OpenAIInvalidResponseError(
                    "OpenAI alternative match had invalid confidence."
                ) from exc

            matches.append(
                AlternativeMatch(
                    title=raw_match["title"].strip(),
                    category=raw_match["category"].strip(),
                    confidence=max(0, min(confidence, 100)),
                    reason=raw_match["reason"].strip(),
                )
            )

        return matches

    def _parse_field_confidence(self, payload: dict[str, Any]) -> dict[str, int]:
        raw_confidence = payload.get("fieldConfidence")
        if not isinstance(raw_confidence, dict):
            return {}

        parsed: dict[str, int] = {}
        for key, value in raw_confidence.items():
            if not isinstance(key, str) or not key.strip():
                continue
            try:
                parsed[key.strip()] = max(0, min(int(value), 100))
            except (TypeError, ValueError):
                raise OpenAIInvalidResponseError(
                    "OpenAI fieldConfidence values must be integers from 0 to 100."
                )
        return parsed

    def _confidence_level(self, value: Any, confidence: int) -> str:
        if isinstance(value, str) and value in {"High", "Medium", "Low"}:
            return value
        if confidence >= 90:
            return "High"
        if confidence >= 70:
            return "Medium"
        return "Low"

    def _parse_string_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise OpenAIInvalidResponseError(
                "OpenAI list fields must be arrays of strings."
            )
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    def _media_type_for(self, image_path: Path) -> str:
        extension = image_path.suffix.lower()
        if extension in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if extension == ".png":
            return "image/png"
        return "application/octet-stream"

    def _prompt_from_payload(self, payload: dict[str, Any]) -> str:
        try:
            return payload["input"][0]["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            return ""


def _estimate_tokens(text: str) -> int:
    normalized = text.strip()
    if not normalized:
        return 0
    return max(1, len(normalized) // 4)


# Product-facing provider alias used by the backend analyze endpoint roadmap.
OpenAiVisionProvider = OpenAIRecognitionProvider
