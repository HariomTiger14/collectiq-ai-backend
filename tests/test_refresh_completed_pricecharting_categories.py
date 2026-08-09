import unittest
from unittest.mock import patch

import httpx

from scripts.refresh_completed_pricecharting_categories import (
    TARGET_CATEGORIES,
    SupabaseRegistryReader,
)


def _reader() -> SupabaseRegistryReader:
    return SupabaseRegistryReader(
        supabase_url="https://example.supabase.co",
        service_role_key="key",
        timeout_seconds=5,
    )


class SupabaseRegistryReaderTest(unittest.TestCase):
    def test_filters_by_source_site_target_categories_and_non_null_console_uid(self) -> None:
        captured_params = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_params.update(request.url.params)
            return httpx.Response(200, json=[])

        reader = _reader()
        with patch("httpx.Client", return_value=httpx.Client(transport=httpx.MockTransport(handler))):
            reader.fetch_refreshable_rows()

        self.assertEqual(captured_params["source_site"], "eq.pricecharting")
        self.assertEqual(captured_params["console_uid"], "not.is.null")
        for category in TARGET_CATEGORIES:
            self.assertIn(category, captured_params["category"])

    def test_paginates_across_multiple_pages(self) -> None:
        pages = [
            [{"registry_id": "1", "console_uid": "a"}, {"registry_id": "2", "console_uid": "b"}],
            [{"registry_id": "3", "console_uid": "c"}],
        ]
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            page = pages[calls["n"]]
            calls["n"] += 1
            return httpx.Response(200, json=page)

        reader = _reader()
        with patch("scripts.refresh_completed_pricecharting_categories.REGISTRY_PAGE_SIZE", 2), patch(
            "httpx.Client", return_value=httpx.Client(transport=httpx.MockTransport(handler))
        ):
            rows = reader.fetch_refreshable_rows()

        self.assertEqual([row["registry_id"] for row in rows], ["1", "2", "3"])
        self.assertEqual(calls["n"], 2)

    def test_stops_immediately_on_empty_first_page(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
        reader = _reader()
        with patch("httpx.Client", return_value=http):
            rows = reader.fetch_refreshable_rows()

        self.assertEqual(rows, [])

    def test_requires_supabase_credentials(self) -> None:
        with self.assertRaises(SystemExit):
            SupabaseRegistryReader(supabase_url="", service_role_key="", timeout_seconds=5)


if __name__ == "__main__":
    unittest.main()
