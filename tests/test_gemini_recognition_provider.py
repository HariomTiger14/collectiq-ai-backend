import base64
import json
import unittest
from typing import Any

from app.services.ai.gemini_recognition_provider import GeminiRecognitionProvider


class FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self.status_code = 200
        self.text = json.dumps(body)
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.response


class GeminiRecognitionProviderTest(unittest.TestCase):
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
