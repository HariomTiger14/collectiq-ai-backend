import json
import unittest

import httpx

from app.services.catalog_image_flags_service import (
    KNOWN_CATEGORIES,
    CatalogImageFlagsError,
    CatalogImageFlagsService,
    SupabaseCatalogImageFlagsRepository,
    UnknownCatalogImageCategoryError,
)


class CatalogImageFlagsServiceTest(unittest.TestCase):
    def test_list_flags_not_configured_returns_all_known_categories_enabled(self) -> None:
        repository = SupabaseCatalogImageFlagsRepository(
            supabase_url="",
            service_role_key="",
        )
        service = CatalogImageFlagsService(repository=repository)

        payload = service.list_flags()

        self.assertTrue(payload["success"])
        self.assertEqual(len(payload["flags"]), len(KNOWN_CATEGORIES))
        categories = {flag["category"] for flag in payload["flags"]}
        self.assertEqual(categories, set(KNOWN_CATEGORIES))
        for flag in payload["flags"]:
            self.assertTrue(flag["enabled"])
            self.assertIsNone(flag["updatedAt"])

    def test_list_flags_configured_merges_repository_rows_with_defaults(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "GET")
            self.assertIn("/rest/v1/catalog_image_source_flags", str(request.url))
            return httpx.Response(
                200,
                json=[
                    {
                        "category": "pokemon",
                        "enabled": False,
                        "updated_at": "2026-08-18T00:00:00+00:00",
                    }
                ],
            )

        repository = SupabaseCatalogImageFlagsRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        service = CatalogImageFlagsService(repository=repository)

        payload = service.list_flags()

        flags_by_category = {flag["category"]: flag for flag in payload["flags"]}
        self.assertEqual(len(flags_by_category), len(KNOWN_CATEGORIES))
        self.assertFalse(flags_by_category["pokemon"]["enabled"])
        self.assertEqual(
            flags_by_category["pokemon"]["updatedAt"], "2026-08-18T00:00:00+00:00"
        )
        # Every other known category falls back to the enabled-by-default shape.
        self.assertTrue(flags_by_category["funko"]["enabled"])
        self.assertIsNone(flags_by_category["funko"]["updatedAt"])

    def test_set_flag_success_not_configured_returns_in_memory_style_flag(self) -> None:
        repository = SupabaseCatalogImageFlagsRepository(
            supabase_url="",
            service_role_key="",
        )
        service = CatalogImageFlagsService(repository=repository)

        payload = service.set_flag(category="pokemon", enabled=False)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["flag"]["category"], "pokemon")
        self.assertFalse(payload["flag"]["enabled"])
        self.assertIsNotNone(payload["flag"]["updatedAt"])

    def test_set_flag_success_configured_returns_repository_row(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "PATCH")
            return httpx.Response(
                200,
                json=[
                    {
                        "category": "lego",
                        "enabled": False,
                        "updated_at": "2026-08-18T01:00:00+00:00",
                    }
                ],
            )

        repository = SupabaseCatalogImageFlagsRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        service = CatalogImageFlagsService(repository=repository)

        payload = service.set_flag(category="lego", enabled=False)

        self.assertTrue(payload["success"])
        self.assertEqual(
            payload["flag"],
            {
                "category": "lego",
                "enabled": False,
                "updatedAt": "2026-08-18T01:00:00+00:00",
            },
        )

    def test_set_flag_unknown_category_raises_without_calling_repository(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=[])

        repository = SupabaseCatalogImageFlagsRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        service = CatalogImageFlagsService(repository=repository)

        with self.assertRaises(UnknownCatalogImageCategoryError):
            service.set_flag(category="not-a-real-category", enabled=True)

        self.assertEqual(calls, [])

    def test_set_flag_normalizes_category_case_and_whitespace(self) -> None:
        repository = SupabaseCatalogImageFlagsRepository(
            supabase_url="",
            service_role_key="",
        )
        service = CatalogImageFlagsService(repository=repository)

        payload = service.set_flag(category="  Pokemon  ", enabled=True)

        self.assertEqual(payload["flag"]["category"], "pokemon")


class SupabaseCatalogImageFlagsRepositoryTest(unittest.TestCase):
    def test_is_configured_reflects_url_and_key_presence(self) -> None:
        configured = SupabaseCatalogImageFlagsRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
        )
        self.assertTrue(configured.is_configured)

        not_configured = SupabaseCatalogImageFlagsRepository(
            supabase_url="",
            service_role_key="",
        )
        self.assertFalse(not_configured.is_configured)

    def test_list_flags_builds_expected_get_request(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=[])

        repository = SupabaseCatalogImageFlagsRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        repository.list_flags()

        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(request.method, "GET")
        self.assertTrue(
            str(request.url).startswith(
                "https://example.supabase.co/rest/v1/catalog_image_source_flags"
            )
        )
        self.assertEqual(
            request.url.params.get("select"), "category,enabled,updated_at"
        )
        self.assertEqual(request.url.params.get("order"), "category.asc")
        self.assertEqual(request.headers["apikey"], "service-role")
        self.assertEqual(request.headers["Authorization"], "Bearer service-role")

    def test_list_flags_raises_on_invalid_response_shape(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": "shape"})

        repository = SupabaseCatalogImageFlagsRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaises(CatalogImageFlagsError):
            repository.list_flags()

    def test_set_flag_builds_expected_patch_request(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json=[
                    {
                        "category": "magic",
                        "enabled": False,
                        "updated_at": "2026-08-18T02:00:00+00:00",
                    }
                ],
            )

        repository = SupabaseCatalogImageFlagsRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = repository.set_flag(category="magic", enabled=False)

        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(request.method, "PATCH")
        self.assertEqual(request.url.params.get("category"), "eq.magic")
        self.assertEqual(request.headers["Prefer"], "return=representation")
        payload = json.loads(request.content)
        self.assertEqual(payload["enabled"], False)
        self.assertIn("updated_at", payload)
        self.assertEqual(
            result,
            {
                "category": "magic",
                "enabled": False,
                "updatedAt": "2026-08-18T02:00:00+00:00",
            },
        )

    def test_set_flag_raises_unknown_category_when_response_row_missing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        repository = SupabaseCatalogImageFlagsRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaises(UnknownCatalogImageCategoryError):
            repository.set_flag(category="does-not-exist", enabled=True)

    def test_request_raises_catalog_image_flags_error_on_http_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"message": "boom"})

        repository = SupabaseCatalogImageFlagsRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaises(CatalogImageFlagsError):
            repository.list_flags()


if __name__ == "__main__":
    unittest.main()
