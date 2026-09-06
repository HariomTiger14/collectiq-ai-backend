"""Backend failures must be readable by the admin browser AND recorded in ops.

Before this, every failure fell into one of two blind spots:

  * An UNHANDLED 500 was recorded in ops_error_events but carried no CORS
    headers, because @app.exception_handler(Exception) is served by
    Starlette's ServerErrorMiddleware -- outside every user middleware,
    including CORS. The browser discarded the response and showed a bare
    "Failed to fetch", so an admin saw a network problem where there was a
    server bug, and the useful envelope was never displayed.

  * A HANDLED 5xx was perfectly readable and recorded nowhere, because only
    unhandled exceptions were captured. The console showed a real failure
    that left no trace in the observability stack built to catch exactly
    that.

Between them there was no failure class that was both visible and recorded,
which is why both live investigations during launch hardening had to query
the database by hand to find out what had actually happened.

The CORS half depends on middleware registration ORDER, which is
counter-intuitive (add_middleware inserts at position 0, so the last
registered is outermost). That is why it is asserted here rather than left to
a comment: move the catch-all below add_middleware(CORSMiddleware) and these
tests fail.
"""

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.services.ops.scrubbing import scrub_secrets


ADMIN_ORIGIN = {"Origin": "https://admin.packlox.com"}

# Probe routes, registered once on the real app so the real middleware stack
# is exercised. Named so they cannot collide with a product route.
_PROBE_UNHANDLED = "/__test_probe_unhandled"
_PROBE_LEAKY = "/__test_probe_leaky"
_PROBE_STATUS = "/__test_probe_status/{status_code}"


@app.get(_PROBE_UNHANDLED)
def _probe_unhandled():
    raise RuntimeError("simulated unhandled failure")


@app.get(_PROBE_LEAKY)
def _probe_leaky():
    raise RuntimeError(
        "provider call failed: https://www.pricecharting.com/api/products?t=SUPERSECRET123&q=x"
    )


@app.get(_PROBE_STATUS)
def _probe_status(status_code: int):
    raise HTTPException(
        status_code=status_code,
        detail={"code": "probe_failure", "message": "probe detail", "retryable": True},
    )


class UnhandledErrorsAreReadableByTheBrowserTest(unittest.TestCase):
    """The 18b half: a 500 the admin console can actually read."""

    def setUp(self) -> None:
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_unhandled_500_carries_cors_headers(self) -> None:
        """Pins middleware order. Fails if the catch-all moves below CORS."""
        with patch("app.main.record_unhandled_error"):
            response = self.client.get(_PROBE_UNHANDLED, headers=ADMIN_ORIGIN)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "https://admin.packlox.com",
            "a 500 without CORS headers is discarded by the browser as "
            "'Failed to fetch' -- the middleware order has regressed",
        )

    def test_unhandled_500_returns_the_same_safe_envelope(self) -> None:
        """No new client contract: the admin portal needs no change."""
        with patch("app.main.record_unhandled_error"):
            response = self.client.get(_PROBE_UNHANDLED, headers=ADMIN_ORIGIN)

        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertEqual(payload["error"]["message"], "Internal server error.")

    def test_unhandled_500_does_not_leak_the_real_error_to_the_client(self) -> None:
        with patch("app.main.record_unhandled_error"):
            response = self.client.get(_PROBE_LEAKY, headers=ADMIN_ORIGIN)

        self.assertNotIn("SUPERSECRET123", response.text)
        self.assertNotIn("pricecharting", response.text.lower())

    def test_unhandled_500_is_recorded(self) -> None:
        with patch("app.main.record_unhandled_error") as record:
            self.client.get(_PROBE_UNHANDLED, headers=ADMIN_ORIGIN)

        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["route"], _PROBE_UNHANDLED)

    def test_a_failing_recorder_cannot_break_the_response(self) -> None:
        """Observability must never take down the thing it observes.

        Specifically: if the recorder raises, that exception would escape the
        middleware and be served by ServerErrorMiddleware -- outside CORS --
        silently reinstating the exact bug this middleware exists to fix.
        """
        with patch(
            "app.main.record_unhandled_error", side_effect=RuntimeError("ops down")
        ):
            response = self.client.get(_PROBE_UNHANDLED, headers=ADMIN_ORIGIN)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "https://admin.packlox.com",
            "a failing recorder must not cost the response its CORS headers",
        )

    def test_a_failing_handled_recorder_cannot_break_the_response(self) -> None:
        with patch(
            "app.main.record_handled_server_error", side_effect=RuntimeError("ops down")
        ):
            response = self.client.get(
                _PROBE_STATUS.format(status_code=503), headers=ADMIN_ORIGIN
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "probe_failure")


class HandledServerErrorsAreRecordedTest(unittest.TestCase):
    """The 18c half: a deliberate 5xx now leaves a trace."""

    def setUp(self) -> None:
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_503_is_recorded(self) -> None:
        with patch("app.main.record_handled_server_error") as record:
            response = self.client.get(
                _PROBE_STATUS.format(status_code=503), headers=ADMIN_ORIGIN
            )

        self.assertEqual(response.status_code, 503)
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["status_code"], 503)

    def test_502_is_recorded(self) -> None:
        with patch("app.main.record_handled_server_error") as record:
            self.client.get(_PROBE_STATUS.format(status_code=502), headers=ADMIN_ORIGIN)
        record.assert_called_once()

    def test_client_errors_are_not_recorded(self) -> None:
        """4xx is the caller being told no -- not a defect, and pure noise."""
        for status_code in (401, 403, 404, 409, 422):
            with self.subTest(status_code=status_code):
                with patch("app.main.record_handled_server_error") as record:
                    response = self.client.get(
                        _PROBE_STATUS.format(status_code=status_code),
                        headers=ADMIN_ORIGIN,
                    )
                self.assertEqual(response.status_code, status_code)
                record.assert_not_called()

    def test_handled_5xx_response_is_unchanged(self) -> None:
        """Recording must not alter what the client receives."""
        with patch("app.main.record_handled_server_error"):
            response = self.client.get(
                _PROBE_STATUS.format(status_code=503), headers=ADMIN_ORIGIN
            )

        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "probe_failure")
        self.assertEqual(payload["error"]["message"], "probe detail")
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "https://admin.packlox.com",
        )


class RecordedContentIsScrubbedTest(unittest.TestCase):
    def test_unhandled_message_and_traceback_are_scrubbed(self) -> None:
        from app.services.ops import observability

        with patch.object(observability, "_post") as post:
            observability.record_unhandled_error(
                route="/probe",
                error=RuntimeError("failed: https://x.test/api?t=SUPERSECRET123&q=1"),
            )

        body = post.call_args.args[1]
        self.assertNotIn("SUPERSECRET123", body["message"])
        self.assertNotIn("SUPERSECRET123", body["stack"] or "")
        self.assertIn("[REDACTED]", body["message"])

    def test_handled_detail_is_scrubbed(self) -> None:
        from app.services.ops import observability

        with patch.object(observability, "_post") as post:
            observability.record_handled_server_error(
                route="/probe",
                status_code=503,
                detail={
                    "code": "provider_failed",
                    "message": "GET https://x.test/a?api_key=LEAKED999 failed",
                },
            )

        body = post.call_args.args[1]
        self.assertNotIn("LEAKED999", body["message"])
        self.assertIn("[REDACTED]", body["message"])

    def test_4xx_is_dropped_inside_the_recorder_too(self) -> None:
        """Defence in depth: the status rule holds even if a caller slips."""
        from app.services.ops import observability

        with patch.object(observability, "_post") as post:
            observability.record_handled_server_error(
                route="/probe", status_code=404, detail={"code": "x", "message": "y"}
            )
        post.assert_not_called()

    def test_scrubber_covers_bearer_and_header_style_secrets(self) -> None:
        out = scrub_secrets("Authorization: Bearer abcdef1234567890TOKEN")
        self.assertNotIn("abcdef1234567890TOKEN", out)
        self.assertIn("[REDACTED]", out)


class SharedScrubberTest(unittest.TestCase):
    def test_scripts_re_export_is_the_same_function(self) -> None:
        """One copy, both paths -- the API path used to have none."""
        from scripts._ops_run_recorder import scrub_secrets as scripts_scrub

        self.assertIs(scripts_scrub, scrub_secrets)


if __name__ == "__main__":
    unittest.main()
