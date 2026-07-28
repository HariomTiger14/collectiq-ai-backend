import unittest

import httpx

from app.services.pricing.base_pricing_provider import PricingProviderRateLimitError
from app.services.pricing.cache import SharedProviderThrottle


class SharedProviderThrottleTest(unittest.TestCase):
    def test_acquire_allows_when_rpc_returns_acquired(self) -> None:
        client = _FakeHttpClient(_FakeResponse([{"acquired": True, "retry_after_ms": 0}]))
        throttle = SharedProviderThrottle(
            1000,
            supabase_url="https://supabase.test",
            service_role_key="service-key",
            client=client,
        )

        throttle.acquire("pricecharting")

        self.assertEqual(client.call_count, 1)
        self.assertEqual(
            client.last_request["json"]["provider_key_arg"],
            "pricecharting",
        )
        self.assertEqual(client.last_request["json"]["min_interval_ms_arg"], 1000)

    def test_acquire_blocks_when_rpc_returns_busy(self) -> None:
        client = _FakeHttpClient(_FakeResponse([{"acquired": False, "retry_after_ms": 750}]))
        throttle = SharedProviderThrottle(
            1000,
            supabase_url="https://supabase.test",
            service_role_key="service-key",
            client=client,
        )

        with self.assertRaises(PricingProviderRateLimitError) as context:
            throttle.acquire("pricecharting")

        self.assertIn("0.75s", str(context.exception))

    def test_acquire_blocks_when_rpc_is_unavailable(self) -> None:
        client = _FakeHttpClient(exception=httpx.ConnectError("offline"))
        throttle = SharedProviderThrottle(
            1000,
            supabase_url="https://supabase.test",
            service_role_key="service-key",
            client=client,
        )

        with self.assertRaises(PricingProviderRateLimitError) as context:
            throttle.acquire("pricecharting")

        self.assertIn("shared throttle unavailable", str(context.exception))


class _FakeResponse:
    def __init__(self, body, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("POST", "https://supabase.test"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._body


class _FakeHttpClient:
    def __init__(self, response: _FakeResponse | None = None, exception=None) -> None:
        self.response = response or _FakeResponse([])
        self.exception = exception
        self.call_count = 0
        self.last_request = None

    def post(self, url: str, **kwargs):
        self.call_count += 1
        self.last_request = {"url": url, **kwargs}
        if self.exception is not None:
            raise self.exception
        return self.response


if __name__ == "__main__":
    unittest.main()
