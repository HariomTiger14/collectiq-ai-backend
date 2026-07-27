import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services.metadata.ebay_metadata_provider import (
    EbayMetadataProvider,
    EbayMetadataUnavailableError,
)


class EbayMetadataProviderTest(unittest.TestCase):
    def test_search_returns_metadata_without_price_fields(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_browse_payload()))
        provider = _provider(client=client)

        response = provider.search("hot wheels skyline", limit=5)

        self.assertEqual(client.call_count, 1)
        self.assertEqual(client.last_request["params"]["q"], "hot wheels skyline")
        self.assertEqual(response.dataUse, "metadata_only")
        self.assertEqual(response.valuationStatus, "not_valuation")
        self.assertEqual(response.count, 1)
        result = response.results[0]
        self.assertEqual(result.itemId, "v1|123|0")
        self.assertEqual(result.title, "Hot Wheels Nissan Skyline")
        self.assertEqual(result.categoryName, "Diecast Vehicles")
        self.assertEqual(result.condition, "New")
        self.assertEqual(result.itemAspects["Brand"], ["Hot Wheels"])
        self.assertNotIn("price", result.model_dump())
        self.assertNotIn("marketValue", result.model_dump())

    def test_short_query_returns_empty_without_calling_ebay(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_browse_payload()))
        provider = _provider(client=client)

        response = provider.search("h", limit=5)

        self.assertEqual(client.call_count, 0)
        self.assertEqual(response.count, 0)

    def test_unconfigured_provider_does_not_call_ebay(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_browse_payload()))
        provider = _provider(access_token="", client=client)

        with self.assertRaises(EbayMetadataUnavailableError):
            provider.search("hot wheels", limit=5)

        self.assertEqual(client.call_count, 0)

    def test_client_credentials_fetch_and_cache_oauth_token(self) -> None:
        client = _FakeHttpClient(
            response=_FakeResponse(body=_browse_payload()),
            post_response=_FakeResponse(
                body={"access_token": "oauth-token", "expires_in": 7200},
            ),
        )
        provider = _provider(
            access_token="",
            client_id="client-id",
            client_secret="client-secret",
            client=client,
        )

        first = provider.search("hot wheels", limit=5)
        second = provider.search("matchbox", limit=5)

        self.assertEqual(first.count, 1)
        self.assertEqual(second.count, 1)
        self.assertEqual(client.post_count, 1)
        self.assertEqual(client.call_count, 2)
        self.assertEqual(
            client.last_request["headers"]["Authorization"],
            "Bearer oauth-token",
        )


class EbayMetadataEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_ebay_metadata_endpoint_returns_metadata_only_results(self) -> None:
        with patch("app.routers.metadata._provider") as provider_factory:
            provider_factory.return_value = _provider(
                client=_FakeHttpClient(response=_FakeResponse(body=_browse_payload()))
            )

            response = self.client.get(
                "/api/metadata/ebay/search?q=hot%20wheels&limit=5"
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["dataUse"], "metadata_only")
        self.assertEqual(payload["valuationStatus"], "not_valuation")
        self.assertNotIn("price", payload["results"][0])
        self.assertNotIn("marketValue", payload["results"][0])


def _provider(
    *,
    access_token: str = "test-token",
    client_id: str = "",
    client_secret: str = "",
    client=None,
) -> EbayMetadataProvider:
    return EbayMetadataProvider(
        access_token=access_token,
        client_id=client_id,
        client_secret=client_secret,
        oauth_token_url="https://api.ebay.com/identity/v1/oauth2/token",
        oauth_scope="https://api.ebay.com/oauth/api_scope",
        browse_api_url="https://api.ebay.com/buy/browse/v1/item_summary/search",
        marketplace_id="EBAY_AU",
        timeout_seconds=1,
        client=client,
    )


def _browse_payload() -> dict:
    return {
        "itemSummaries": [
            {
                "itemId": "v1|123|0",
                "title": "Hot Wheels Nissan Skyline",
                "price": {"value": "199.99", "currency": "AUD"},
                "category": {
                    "categoryId": "180506",
                    "categoryName": "Diecast Vehicles",
                },
                "condition": "New",
                "itemWebUrl": "https://example.test/item/123",
                "image": {"imageUrl": "https://example.test/image.jpg"},
                "itemLocation": {"country": "AU"},
                "itemCreationDate": "2026-07-27T00:00:00Z",
                "itemEndDate": "2026-08-01T00:00:00Z",
                "itemAspects": [
                    {"name": "Brand", "value": ["Hot Wheels"]},
                    {"name": "Vehicle Make", "value": ["Nissan"]},
                ],
            }
        ]
    }


class _FakeResponse:
    def __init__(self, status_code: int = 200, body=None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class _FakeHttpClient:
    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        post_response: _FakeResponse | None = None,
    ) -> None:
        self.response = response or _FakeResponse()
        self.post_response = post_response or _FakeResponse()
        self.call_count = 0
        self.post_count = 0
        self.last_request = {}

    def get(self, url, *, headers, params, timeout):
        self.call_count += 1
        self.last_request = {
            "url": url,
            "headers": headers,
            "params": params,
            "timeout": timeout,
        }
        return self.response

    def post(self, url, *, headers, data, timeout):
        self.post_count += 1
        return self.post_response


if __name__ == "__main__":
    unittest.main()

