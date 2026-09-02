"""Credentials must never reach the run ledger.

Real incident, ops_cron_runs 2026-08-29: pricecharting-csv-refresh took a 503
from the provider, and httpx's HTTPStatusError carries the full request URL --
including the ?t=<token> these providers authenticate with. The recorder stored
that traceback verbatim, so a live API key sat in the database in plaintext,
readable by anything with database access and copied again into
ops_error_events.

_redact_token() in backfill_pricecharting_sets.py did not help: it covers only
print() calls in that one file and has to be handed the token. Scrubbing at the
recorder is what makes every job safe by default.
"""

import unittest

from scripts._ops_run_recorder import scrub_secrets


class ScrubSecretsTest(unittest.TestCase):
    def test_redacts_the_token_param_that_actually_leaked(self):
        raw = (
            "httpx.HTTPStatusError: Server error '503 Service Unavailable' for "
            "url 'https://www.pricecharting.com/price-guide/download-custom"
            "?t=c73b9d5b6d31654ad62d3d2c699d55d44022f39c'"
        )
        out = scrub_secrets(raw)
        self.assertNotIn("c73b9d5b6d31654ad62d3d2c699d55d44022f39c", out)
        self.assertIn("?t=[REDACTED]", out)

    def test_redacts_common_credential_parameter_names(self):
        for param in ("key", "token", "api_key", "api-key", "apikey",
                      "access_token", "password", "secret"):
            with self.subTest(param=param):
                out = scrub_secrets(f"https://x.test/a?{param}=SUPERSECRET99")
                self.assertNotIn("SUPERSECRET99", out)
                self.assertIn("[REDACTED]", out)

    def test_is_case_insensitive(self):
        self.assertNotIn("SUPERSECRET99", scrub_secrets("https://x.test/a?API_KEY=SUPERSECRET99"))

    def test_keeps_non_secret_parameters_readable(self):
        # A scrubbed traceback still has to be useful for debugging.
        out = scrub_secrets("https://x.test/a?t=SECRET&console-uids=G1,G2&page=3")
        self.assertNotIn("SECRET", out)
        self.assertIn("console-uids=G1,G2", out)
        self.assertIn("page=3", out)

    def test_redacts_every_occurrence(self):
        out = scrub_secrets("a?t=AAAA111122223333 then b?api_key=BBBB444455556666")
        self.assertNotIn("AAAA111122223333", out)
        self.assertNotIn("BBBB444455556666", out)
        self.assertEqual(out.count("[REDACTED]"), 2)

    def test_leaves_ordinary_text_alone(self):
        for text in ("", "no urls here", "https://x.test/a?page=2&sort=name"):
            self.assertEqual(scrub_secrets(text), text)

    def test_stops_at_the_url_boundary(self):
        # The value must not swallow the closing quote or trailing prose,
        # or the redacted traceback becomes unreadable.
        out = scrub_secrets("for url 'https://x.test/a?t=SECRETVALUE1234' extra context")
        self.assertNotIn("SECRETVALUE1234", out)
        self.assertIn("extra context", out)
        self.assertIn("'", out)


if __name__ == "__main__":
    unittest.main()
