import unittest

import httpx

from app.services.pricing.kicksdb_catalog_matcher import KicksDBCatalogMatcher


class KicksDBCatalogMatcherTest(unittest.TestCase):
    def test_is_configured_requires_both_url_and_key(self) -> None:
        self.assertFalse(
            KicksDBCatalogMatcher(supabase_url="", service_role_key="key", timeout_seconds=5).is_configured()
        )
        self.assertFalse(
            KicksDBCatalogMatcher(
                supabase_url="https://example.supabase.co", service_role_key="", timeout_seconds=5
            ).is_configured()
        )
        self.assertTrue(
            KicksDBCatalogMatcher(
                supabase_url="https://example.supabase.co", service_role_key="key", timeout_seconds=5
            ).is_configured()
        )

    def test_candidates_returns_empty_list_when_not_configured(self) -> None:
        matcher = KicksDBCatalogMatcher(supabase_url="", service_role_key="", timeout_seconds=5)
        self.assertEqual(matcher.candidates(), [])

    def test_candidates_loads_and_caches_rows(self) -> None:
        client = _FakeClient(response=_FakeResponse(body=[{"kicksdb_id": "1", "title": "Shoe"}]))
        matcher = KicksDBCatalogMatcher(
            supabase_url="https://example.supabase.co",
            service_role_key="key",
            timeout_seconds=5,
            cache_ttl_seconds=3600,
            client=client,
        )

        first = matcher.candidates()
        second = matcher.candidates()

        self.assertEqual(first, [{"kicksdb_id": "1", "title": "Shoe"}])
        self.assertEqual(second, first)
        self.assertEqual(client.call_count, 1)  # second call served from cache, no new request

    def test_candidates_refreshes_after_ttl_expires(self) -> None:
        client = _FakeClient(response=_FakeResponse(body=[{"kicksdb_id": "1", "title": "Shoe"}]))
        matcher = KicksDBCatalogMatcher(
            supabase_url="https://example.supabase.co",
            service_role_key="key",
            timeout_seconds=5,
            cache_ttl_seconds=0,
            client=client,
        )

        matcher.candidates()
        matcher.candidates()

        self.assertEqual(client.call_count, 2)

    def test_returns_empty_list_on_http_error(self) -> None:
        # A catalog-load failure must not break pricing entirely -- every
        # scan should just fall through to the existing live-lookup path.
        client = _FakeClient(exception=httpx.ConnectError("boom"))
        matcher = KicksDBCatalogMatcher(
            supabase_url="https://example.supabase.co",
            service_role_key="key",
            timeout_seconds=5,
            client=client,
        )
        self.assertEqual(matcher.candidates(), [])

    def test_returns_empty_list_when_response_is_not_a_list(self) -> None:
        client = _FakeClient(response=_FakeResponse(body={"unexpected": "shape"}))
        matcher = KicksDBCatalogMatcher(
            supabase_url="https://example.supabase.co", service_role_key="key", timeout_seconds=5, client=client
        )
        self.assertEqual(matcher.candidates(), [])


class _FakeClient:
    def __init__(self, *, response=None, exception=None) -> None:
        self.response = response
        self.exception = exception
        self.call_count = 0

    def get(self, url, **kwargs):
        self.call_count += 1
        if self.exception is not None:
            raise self.exception
        return self.response

    def close(self):
        pass


class _FakeResponse:
    def __init__(self, *, body=None) -> None:
        self.body = body if body is not None else []

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


if __name__ == "__main__":
    unittest.main()
