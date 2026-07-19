import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth.signup_start_guard import (
    SignupStartGuardDecision,
    SignupStartGuardError,
    SignupStartThrottle,
    SupabaseSignupStartGuard,
)


class AuthSignupStartGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_fresh_email_is_allowed_without_exposing_secrets(self) -> None:
        with patch(
            "app.routers.auth.SupabaseSignupStartGuard",
            return_value=SimpleNamespace(
                start=lambda email: SignupStartGuardDecision(
                    safe_for_account_creation=True
                )
            ),
        ), patch("app.routers.auth._throttle", SignupStartThrottle()):
            response = self.client.post(
                "/auth/signup-start",
                json={"email": " NewUser@Example.com "},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["safeForAccountCreation"])
        self.assertEqual(payload["delivery"], "otpCode")
        serialized = json.dumps(payload).lower()
        self.assertNotIn("service_role", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("token", serialized)

    def test_confirmed_existing_email_is_blocked_ambiguously(self) -> None:
        with patch(
            "app.routers.auth.SupabaseSignupStartGuard",
            return_value=SimpleNamespace(
                start=lambda email: SignupStartGuardDecision(
                    safe_for_account_creation=False
                )
            ),
        ), patch("app.routers.auth._throttle", SignupStartThrottle()):
            response = self.client.post(
                "/auth/signup-start",
                json={"email": "collector@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["safeForAccountCreation"])
        self.assertNotIn("exists", json.dumps(payload).lower())
        self.assertNotIn("registered", json.dumps(payload).lower())

    def test_config_or_supabase_failure_is_retryable(self) -> None:
        def fail(email):
            raise SignupStartGuardError("Supabase admin lookup failed.")

        with patch(
            "app.routers.auth.SupabaseSignupStartGuard",
            return_value=SimpleNamespace(start=fail),
        ), patch("app.routers.auth._throttle", SignupStartThrottle()):
            response = self.client.post(
                "/auth/signup-start",
                json={"email": "new@example.com"},
            )

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["code"], "signup_start_unavailable")
        self.assertTrue(error["retryable"])

    def test_supabase_admin_lookup_blocks_matching_confirmed_account(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.headers["authorization"],
                "Bearer test-placeholder-key",
            )
            self.assertEqual(request.url.params["email"], "collector@example.com")
            return httpx.Response(
                200,
                json={
                    "users": [
                        {
                            "email": "collector@example.com",
                            "email_confirmed_at": "2026-07-01T00:00:00Z",
                        }
                    ]
                },
            )

        guard = SupabaseSignupStartGuard(
            supabase_url="https://example.supabase.co",
            service_role_key="test-placeholder-key",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        decision = guard.start(email="Collector@Example.com")

        self.assertFalse(decision.safe_for_account_creation)

    def test_supabase_admin_lookup_allows_fresh_email(self) -> None:
        guard = SupabaseSignupStartGuard(
            supabase_url="https://example.supabase.co",
            service_role_key="test-placeholder-key",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"users": []})
                )
            ),
        )

        decision = guard.start(email="new@example.com")

        self.assertTrue(decision.safe_for_account_creation)

    def test_supabase_admin_lookup_allows_unconfirmed_matching_user(self) -> None:
        guard = SupabaseSignupStartGuard(
            supabase_url="https://example.supabase.co",
            service_role_key="test-placeholder-key",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "users": [
                                {
                                    "email": "new@example.com",
                                    "email_confirmed_at": None,
                                    "confirmed_at": None,
                                }
                            ]
                        },
                    )
                )
            ),
        )

        decision = guard.start(email="new@example.com")

        self.assertTrue(decision.safe_for_account_creation)
