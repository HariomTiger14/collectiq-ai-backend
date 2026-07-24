import base64
import json
import unittest
from typing import Any

from app.services.ai.gemini_recognition_provider import GeminiRecognitionProvider


class FakeResponse:
    def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = json.dumps(body)
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class FakeClient:
    def __init__(self, response: FakeResponse | list[FakeResponse]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append(kwargs)
        return self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]


class GeminiRecognitionProviderTest(unittest.TestCase):
    def test_payload_includes_response_schema_for_required_scan_fields(self) -> None:
        provider = GeminiRecognitionProvider(
            api_key="test-key",
            model="gemini-test",
            client=FakeClient(FakeResponse({})),
        )

        payload = provider._build_gemini_payload(
            image_base64=base64.b64encode(b"jpeg-bytes").decode("ascii"),
            mime_type="image/jpeg",
            prompt_context={
                "imageSource": "test",
                "requestedCategory": "video games",
                "fileName": "mario-kart.jpg",
                "mimeType": "image/jpeg",
                "appVersion": "test",
            },
        )

        generation_config = payload["generationConfig"]
        text_format = generation_config["responseFormat"]["text"]
        schema = text_format["schema"]
        self.assertEqual(text_format["mimeType"], "application/json")
        self.assertIn("title", schema["required"])
        self.assertIn("category", schema["required"])
        self.assertIn("confidence", schema["required"])
        self.assertIn("alternativeMatches", schema["required"])
        self.assertEqual(schema["properties"]["title"]["type"], "string")

    def test_retries_without_schema_when_gemini_rejects_structured_output(self) -> None:
        gemini_output = {
            "title": "Mario Kart 8 Deluxe",
            "category": "Video Game",
            "confidence": 82,
            "estimatedValue": 0,
            "condition": "Unknown",
            "recommendation": "Confirm region and cartridge/case condition.",
            "description": "Nintendo Switch Mario Kart 8 Deluxe cover art.",
            "detectedObjects": ["Nintendo Switch case", "Mario Kart 8 Deluxe"],
            "primaryMatch": "Mario Kart 8 Deluxe Nintendo Switch",
            "alternativeMatches": [],
            "confidenceExplanation": "Visible title and platform.",
            "detectionQuality": "Good",
            "aiReasoning": "The front cover title is readable.",
        }
        client = FakeClient(
            [
                FakeResponse({"error": {"message": "schema rejected"}}, status_code=400),
                FakeResponse(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [{"text": json.dumps(gemini_output)}],
                                },
                            }
                        ],
                    }
                ),
            ]
        )
        provider = GeminiRecognitionProvider(
            api_key="test-key",
            model="gemini-test",
            client=client,
        )

        result = provider.recognize_api_payload(
            request_metadata={
                "imageSource": "test",
                "requestedCategory": "video games",
                "appVersion": "test",
            },
            image_payload={
                "fileName": "mario-kart.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 20,
                "imageSource": "test",
                "localFilePath": "/tmp/missing-mario-kart.jpg",
                "base64Image": base64.b64encode(b"jpeg-bytes").decode("ascii"),
            },
        )

        self.assertEqual(result.title, "Mario Kart 8 Deluxe")
        self.assertEqual(len(client.requests), 2)
        self.assertIn("responseFormat", client.requests[0]["json"]["generationConfig"])
        self.assertNotIn("responseFormat", client.requests[1]["json"]["generationConfig"])
        self.assertEqual(
            client.requests[1]["json"]["generationConfig"]["response_mime_type"],
            "application/json",
        )

    def test_partial_alternative_matches_are_normalized(self) -> None:
        gemini_output = {
            "title": "Charizard",
            "category": "Pokemon Card",
            "confidence": 88,
            "estimatedValue": 0,
            "condition": "Unknown",
            "recommendation": "Confirm print run and condition before saving.",
            "description": "Base Set Charizard Pokemon card.",
            "detectedObjects": ["Pokemon card", "Charizard"],
            "primaryMatch": "Charizard Base Set 4/102",
            "alternativeMatches": [
                {
                    "category": "Pokemon Card",
                    "confidence": 70,
                    "reason": "Same Pokemon and card layout.",
                }
            ],
            "confidenceExplanation": "Clear card front.",
            "detectionQuality": "Good",
            "aiReasoning": "The card title and number are visible.",
        }
        response = FakeResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": json.dumps(gemini_output)},
                            ],
                        },
                    }
                ],
            }
        )
        provider = GeminiRecognitionProvider(
            api_key="test-key",
            model="gemini-test",
            client=FakeClient(response),
        )

        result = provider.recognize_api_payload(
            request_metadata={
                "imageSource": "test",
                "requestedCategory": "pokemon card",
                "appVersion": "test",
            },
            image_payload={
                "fileName": "card.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 20,
                "imageSource": "test",
                "localFilePath": "/tmp/card.jpg",
                "base64Image": base64.b64encode(b"jpeg-bytes").decode("ascii"),
            },
        )

        self.assertEqual(result.title, "Charizard")
        self.assertEqual(len(result.alternativeMatches), 3)
        self.assertEqual(result.alternativeMatches[0].title, "Charizard")
        self.assertEqual(result.alternativeMatches[0].category, "Pokemon Card")


if __name__ == "__main__":
    unittest.main()
