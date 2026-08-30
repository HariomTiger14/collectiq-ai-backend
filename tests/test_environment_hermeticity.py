"""Guard: local developer configuration must never reach the test suite.

Without this, contamination is invisible until it is expensive. On
2026-08-31 a developer .env produced 47 failures on clean `main` that looked
like product regressions -- one asserted an estimated market value of 420 and
got 150.0 -- and the tests were quietly talking to production Supabase while
doing it. Earlier, the same class of leak wrote test rows into the production
ops ledger, which was only noticed because a job appeared to have failed with
the fixture string "boom".

The isolation lives in tests/conftest.py. These tests assert its effect, so a
regression in it surfaces as one clear failure here instead of dozens of
confusing ones elsewhere.

Deliberately asserting on `settings` rather than os.environ: Settings reads
the environment when the class is DEFINED, so it is the honest record of what
the application actually saw at import time. Clearing os.environ after that
point would look clean here while the app had already been configured.
"""

import os
import unittest

from app.core.config import settings


class EnvironmentHermeticityTest(unittest.TestCase):
    def test_supabase_is_not_configured_during_tests(self) -> None:
        # 41 of the 47 failures came from these two being present: with real
        # credentials the service layer bypassed its fixtures and issued live
        # requests against production.
        self.assertEqual(
            settings.supabase_url,
            "",
            "SUPABASE_URL leaked into the test process -- tests would run "
            "against real Supabase. See tests/conftest.py.",
        )
        self.assertEqual(
            settings.supabase_service_role_key,
            "",
            "SUPABASE_SERVICE_ROLE_KEY leaked into the test process. "
            "See tests/conftest.py.",
        )

    def test_third_party_provider_credentials_are_not_configured(self) -> None:
        # A configured provider changes which branch the code under test
        # takes, so tests covering the unconfigured path silently stop
        # testing it. PRICECHARTING_API_KEY alone accounted for 6 failures.
        for name in (
            "PRICECHARTING_API_KEY",
            "EBAY_CLIENT_ID",
            "EBAY_CLIENT_SECRET",
            "RAWG_API_KEY",
            "KICKSDB_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
        ):
            with self.subTest(variable=name):
                self.assertIn(
                    os.getenv(name, ""),
                    ("", None),
                    f"{name} leaked into the test process. See tests/conftest.py.",
                )

    def test_dotenv_loading_is_neutralised(self) -> None:
        # app/core/config.py calls load_dotenv() at import with an absolute
        # path, so neither cwd nor a cleaned shell prevents the file being
        # read. conftest stubs the function itself; if that stops working,
        # every other guard here becomes load-order dependent.
        import dotenv

        self.assertFalse(
            dotenv.load_dotenv("/nonexistent/.env"),
            "dotenv.load_dotenv is not stubbed; a developer .env can reach "
            "the test process. See tests/conftest.py.",
        )

    def test_observability_writes_are_disabled(self) -> None:
        # Independent of the credentials above: this is what stops a test run
        # writing rows into the production ops ledger if Supabase settings are
        # ever repopulated mid-run.
        from app.services.ops.observability import _observability_writes_disabled

        self.assertTrue(_observability_writes_disabled())
