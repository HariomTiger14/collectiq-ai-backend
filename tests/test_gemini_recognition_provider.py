import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from app.services.ai.gemini_recognition_provider import (
    GeminiInvalidResponseError,
    GeminiProviderError,
    GeminiRecognitionProvider,
    GeminiTimeoutError,
)
from app.services.ai.openai_recognition_provider import AIProviderNotConfiguredError


class FakeGeminiResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        body: dict[str, Any] | None = None,
        text: str = "",
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = text
        self._json_error = json_error

    def json(self) -> dict[str, Any]:
        if self._json_error is not None:
            raise self._json_error
        return self._body


class FakeGeminiClient:
    def __init__(
        self,
        response: FakeGeminiResponse | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.response = response
        self.exception = exception
        self.last_request: dict[str, Any] | None = None

    def post(self, url: str, **kwargs: Any) -> FakeGeminiResponse:
        self.last_request = {"url": url, **kwargs}
        if self.exception is not None:
            raise self.exception
        if self.response is None:
            raise AssertionError("FakeGeminiClient requires a response or exception.")
        return self.response


class GeminiRecognitionProviderTest(unittest.TestCase):
    def test_missing_api_key_raises_configuration_error(self) -> None:
        provider = GeminiRecognitionProvider(api_key="", client=FakeGeminiClient())

        with self.assertRaises(AIProviderNotConfiguredError):
            provider.recognize(Path("uploads/card.png"))

    def test_recognize_api_payload_returns_structured_result(self) -> None:
        client = FakeGeminiClient(
            response=FakeGeminiResponse(body=_gemini_response(_gemini_output()))
        )
        provider = GeminiRecognitionProvider(
            api_key="gemini-key",
            model="gemini-test",
            client=client,
        )

        result = provider.recognize_api_payload(
            request_metadata={
                "imageSource": "gallery",
                "requestedCategory": "Coin",
                "appVersion": "1.0.0",
            },
            image_payload={
                "fileName": "coin.png",
                "mimeType": "image/png",
                "base64Image": base64.b64encode(b"image-bytes").decode("ascii"),
            },
        )

        self.assertEqual(result.title, "1921 Morgan Silver Dollar")
        self.assertEqual(result.category, "Coin")
        self.assertEqual(result.confidence, 83)
        self.assertEqual(result.estimatedValue, 140)
        self.assertEqual(result.aiProvider, "gemini")
        self.assertEqual(len(result.alternativeMatches), 3)
        self.assertEqual(result.fieldConfidence["itemName"], 86)
        self.assertIsNotNone(client.last_request)
        self.assertIn("gemini-test:generateContent", client.last_request["url"])
        self.assertNotIn("gemini-key", json.dumps(client.last_request["json"]))
        parts = client.last_request["json"]["contents"][0]["parts"]
        self.assertIn("requestedCategory=Coin", parts[0]["text"])
        inline_parts = [part for part in parts if "inline_data" in part]
        self.assertEqual(inline_parts[0]["inline_data"]["mime_type"], "image/png")

    def test_mapping_handles_missing_fields_safely(self) -> None:
        client = FakeGeminiClient(
            response=FakeGeminiResponse(
                body=_gemini_response(
                    {
                        "title": "Unknown collectible",
                        "category": "Other",
                        "confidence": 37,
                    }
                )
            )
        )
        provider = GeminiRecognitionProvider(api_key="gemini-key", client=client)

        result = provider.recognize_api_payload(
            request_metadata={},
            image_payload={
                "fileName": "item.jpg",
                "mimeType": "image/jpeg",
                "base64Image": base64.b64encode(b"image-bytes").decode("ascii"),
            },
        )

        self.assertEqual(result.title, "Unknown collectible")
        self.assertEqual(result.category, "Other")
        self.assertEqual(result.condition, "Unknown")
        self.assertEqual(result.estimatedValue, 0)
        self.assertEqual(result.confidenceLevel, "Low")
        self.assertEqual(len(result.alternativeMatches), 3)

    def test_recognize_reads_local_file(self) -> None:
        client = FakeGeminiClient(
            response=FakeGeminiResponse(body=_gemini_response(_gemini_output()))
        )
        provider = GeminiRecognitionProvider(api_key="gemini-key", client=client)

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "card.jpg"
            image_path.write_bytes(b"image-bytes")

            result = provider.recognize(image_path)

        self.assertEqual(result.aiProvider, "gemini")
        self.assertEqual(result.title, "1921 Morgan Silver Dollar")

    def test_http_failure_raises_provider_error(self) -> None:
        provider = GeminiRecognitionProvider(
            api_key="gemini-key",
            client=FakeGeminiClient(
                response=FakeGeminiResponse(status_code=500, text="server error")
            ),
        )

        with self.assertRaises(GeminiProviderError):
            provider.recognize_api_payload(
                request_metadata={},
                image_payload={
                    "fileName": "item.jpg",
                    "mimeType": "image/jpeg",
                    "base64Image": base64.b64encode(b"image-bytes").decode("ascii"),
                },
            )

    def test_timeout_raises_timeout_error(self) -> None:
        provider = GeminiRecognitionProvider(
            api_key="gemini-key",
            client=FakeGeminiClient(exception=httpx.TimeoutException("slow")),
        )

        with self.assertRaises(GeminiTimeoutError):
            provider.recognize_api_payload(
                request_metadata={},
                image_payload={
                    "fileName": "item.jpg",
                    "mimeType": "image/jpeg",
                    "base64Image": base64.b64encode(b"image-bytes").decode("ascii"),
                },
            )

    def test_invalid_json_raises_invalid_response_error(self) -> None:
        provider = GeminiRecognitionProvider(
            api_key="gemini-key",
            client=FakeGeminiClient(
                response=FakeGeminiResponse(
                    body={
                        "candidates": [
                            {"content": {"parts": [{"text": "not-json"}]}}
                        ]
                    }
                )
            ),
        )

        with self.assertRaises(GeminiInvalidResponseError):
            provider.recognize_api_payload(
                request_metadata={},
                image_payload={
                    "fileName": "item.jpg",
                    "mimeType": "image/jpeg",
                    "base64Image": base64.b64encode(b"image-bytes").decode("ascii"),
                },
            )


def _gemini_response(output: dict) -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(output),
                        }
                    ]
                }
            }
        ]
    }


def _gemini_output() -> dict:
    return {
        "title": "1921 Morgan Silver Dollar",
        "category": "Coin",
        "confidence": 83,
        "estimatedValue": 140,
        "condition": "Very Fine",
        "recommendation": "Store in a non-PVC holder.",
        "description": "Silver dollar with classic Morgan profile.",
        "detectedObjects": ["Coin", "Silver"],
        "fieldConfidence": {"itemName": 86, "category": 90},
        "confidenceLevel": "Medium",
        "lowConfidenceReasons": ["Mint mark is not fully visible."],
        "imageQualityIssues": ["glare/reflections"],
        "scanRecommendations": ["Retake the image without glare."],
        "primaryMatch": "1921 Morgan Silver Dollar",
        "alternativeMatches": [
            {
                "title": "Peace Silver Dollar",
                "category": "Coin",
                "confidence": 70,
                "reason": "Similar silver dollar format.",
            },
            {
                "title": "1878 Morgan Silver Dollar",
                "category": "Coin",
                "confidence": 64,
                "reason": "Same design family with uncertain date.",
            },
            {
                "title": "American Silver Eagle",
                "category": "Coin",
                "confidence": 48,
                "reason": "Silver coin appearance overlaps.",
            },
        ],
        "confidenceExplanation": "Morgan dollar design cues are visible.",
        "detectionQuality": "Fair - reflective surface obscures details.",
        "aiReasoning": "The portrait and silver dollar format indicate Morgan dollar.",
        "year": "1921",
        "brand": "United States Mint",
        "setName": None,
        "series": "Morgan Dollar",
        "cardNumber": None,
        "playerOrCharacter": None,
        "rarity": None,
        "estimatedGrade": "Very Fine",
        "language": None,
        "edition": None,
        "country": "United States",
        "mint": "Philadelphia",
        "material": "Silver",
        "notes": "Do not clean.",
    }


if __name__ == "__main__":
    unittest.main()
