import tempfile
import unittest
from pathlib import Path

from scripts.validate_real_analysis import (
    build_payload,
    format_summary,
    summarize_response,
)


class ValidationToolkitTest(unittest.TestCase):
    def test_build_payload_includes_base64_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "card.jpg"
            image_path.write_bytes(b"image-bytes")

            payload = build_payload(
                image_path=image_path,
                image_source="validation",
                requested_category="Pokemon Card",
            )

        self.assertEqual(payload["request"]["requestedCategory"], "Pokemon Card")
        self.assertEqual(payload["image"]["fileName"], "card.jpg")
        self.assertEqual(payload["image"]["mimeType"], "image/jpeg")
        self.assertEqual(payload["image"]["sizeBytes"], len(b"image-bytes"))
        self.assertTrue(payload["image"]["base64Image"])

    def test_summarize_response_parses_sample_backend_response(self) -> None:
        summary = summarize_response(_sample_response(), request_latency_ms=321)

        self.assertEqual(summary["itemName"], "1999 Pokemon Charizard Holo")
        self.assertEqual(summary["category"], "Pokemon Card")
        self.assertEqual(summary["pricingSource"], "eBay Browse API")
        self.assertEqual(summary["fallbackUsed"], "no")
        self.assertEqual(summary["requestLatencyMs"], 321)
        self.assertEqual(summary["imageQualityWarnings"], ["glare/reflections"])
        self.assertEqual(summary["alternatives"][0]["title"], "2016 Evolutions Charizard")

    def test_format_summary_outputs_validation_fields(self) -> None:
        summary = summarize_response(_sample_response(), request_latency_ms=321)

        output = format_summary(summary)

        self.assertIn("Item name: 1999 Pokemon Charizard Holo", output)
        self.assertIn("Pricing source: eBay Browse API", output)
        self.assertIn("Fallback used: no", output)
        self.assertIn("Image quality warnings:", output)
        self.assertIn("Alternatives:", output)


def _sample_response() -> dict:
    return {
        "itemName": "1999 Pokemon Charizard Holo",
        "category": "Pokemon Card",
        "confidence": 94,
        "confidenceLevel": "High",
        "estimatedValue": 1850,
        "lowEstimate": 1600,
        "highEstimate": 2200,
        "marketSummary": {
            "sources": ["eBay Browse API"],
        },
        "imageQualityIssues": ["glare/reflections"],
        "alternatives": [
            {
                "title": "2016 Evolutions Charizard",
                "category": "Pokemon Card",
                "confidence": 68,
            }
        ],
        "diagnostics": {
            "pricingFallbackUsed": False,
            "pricingFallbackReason": "",
            "totalLatencyMs": 1100,
            "aiLatencyMs": 800,
            "pricingProviderLatencyMs": 120,
            "confidenceLevel": "High",
        },
    }


if __name__ == "__main__":
    unittest.main()
