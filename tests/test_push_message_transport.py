"""How a push message reaches the two admin send routes.

Title and body used to be query parameters only, so the full text of every
push an admin sent was written into request logs, proxy and CDN access logs,
and the browser's own history. A URL is not a private channel, and a push
body can carry anything an operator types about a user.

These routes now accept a JSON body. Query parameters keep working: the
console sends them until its own change deploys, and the deploy order is not
something this router can assume. Body wins where both are supplied.

The subject here is transport and validation, not delivery -- the service is
stubbed, and what it was called with is the assertion.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from tests.test_admin_route_permissions import _SummaryStub, as_role


AUTH = {"Authorization": "Bearer supabase-session"}

BROADCAST = "/admin/push/broadcast"
DIRECT = "/admin/push/users/user-1/send"


class BroadcastTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def _send(self, url: str, json_body=None):
        with as_role("admin"), patch("app.routers.push.PriceAlertPushService") as service:
            service.return_value.dispatch_broadcast.return_value = _SummaryStub(
                {"segment": "all", "attemptedDeliveries": 0}
            )
            response = self.client.post(url, headers=AUTH, json=json_body)
        return response, service.return_value.dispatch_broadcast

    def test_a_json_body_keeps_the_message_out_of_the_url(self) -> None:
        response, dispatch = self._send(
            f"{BROADCAST}?dryRun=true",
            {"segment": "all", "title": "Heads up", "body": "New prices"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        dispatch.assert_called_once_with(
            segment="all", title="Heads up", body="New prices", dry_run=True
        )

    def test_query_parameters_still_work(self) -> None:
        """The console sends these until its own deploy lands. A release that
        breaks them takes broadcasts down in between."""
        response, dispatch = self._send(
            f"{BROADCAST}?segment=pro&title=Heads%20up&body=New%20prices&dryRun=true"
        )

        self.assertEqual(response.status_code, 200, response.text)
        dispatch.assert_called_once_with(
            segment="pro", title="Heads up", body="New prices", dry_run=True
        )

    def test_the_body_wins_when_both_are_supplied(self) -> None:
        response, dispatch = self._send(
            f"{BROADCAST}?segment=all&title=from-query&body=from-query&dryRun=true",
            {"segment": "pro", "title": "from-body", "body": "also-from-body"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        dispatch.assert_called_once_with(
            segment="pro", title="from-body", body="also-from-body", dry_run=True
        )

    def test_a_missing_field_is_refused_rather_than_sent_empty(self) -> None:
        for payload in (
            {"segment": "all", "body": "no title"},
            {"segment": "all", "title": "no body"},
            {"title": "no segment", "body": "no segment"},
        ):
            with self.subTest(payload=sorted(payload)):
                response, dispatch = self._send(f"{BROADCAST}?dryRun=true", payload)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(
                    response.json()["error"]["code"], "push_field_required"
                )
                dispatch.assert_not_called()

    def test_a_blank_field_is_refused_too(self) -> None:
        response, dispatch = self._send(
            f"{BROADCAST}?dryRun=true", {"segment": "all", "title": "   ", "body": "x"}
        )
        self.assertEqual(response.status_code, 422, response.text)
        dispatch.assert_not_called()

    def test_length_limits_still_apply_through_the_body(self) -> None:
        """Moving off the query string must not drop the bounds FCM needs."""
        response, dispatch = self._send(
            f"{BROADCAST}?dryRun=true",
            {"segment": "all", "title": "x" * 121, "body": "fine"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        dispatch.assert_not_called()

    def test_an_unknown_segment_is_still_rejected(self) -> None:
        response, dispatch = self._send(
            f"{BROADCAST}?dryRun=true",
            {"segment": "everyone", "title": "t", "body": "b"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        dispatch.assert_not_called()

    def test_dry_run_still_defaults_to_true(self) -> None:
        """A body-carrying call that forgets dryRun must not send for real."""
        response, dispatch = self._send(
            BROADCAST, {"segment": "all", "title": "t", "body": "b"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(dispatch.call_args.kwargs["dry_run"], True)


class DirectSendTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def _send(self, url: str, json_body=None):
        with as_role("admin"), patch("app.routers.push.PriceAlertPushService") as service:
            service.return_value.dispatch_to_user.return_value = _SummaryStub(
                {"attemptedDeliveries": 0}
            )
            response = self.client.post(url, headers=AUTH, json=json_body)
        return response, service.return_value.dispatch_to_user

    def test_a_json_body_carries_the_message_and_device(self) -> None:
        response, dispatch = self._send(
            f"{DIRECT}?dryRun=true",
            {"title": "Hello", "body": "About your scan", "deviceId": "device-9"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        dispatch.assert_called_once_with(
            user_id="user-1",
            title="Hello",
            body="About your scan",
            device_id="device-9",
            dry_run=True,
        )

    def test_query_parameters_still_work(self) -> None:
        response, dispatch = self._send(
            f"{DIRECT}?title=Hello&body=About%20your%20scan&deviceId=device-9&dryRun=true"
        )

        self.assertEqual(response.status_code, 200, response.text)
        dispatch.assert_called_once_with(
            user_id="user-1",
            title="Hello",
            body="About your scan",
            device_id="device-9",
            dry_run=True,
        )

    def test_omitting_the_device_sends_to_every_device(self) -> None:
        response, dispatch = self._send(
            f"{DIRECT}?dryRun=true", {"title": "Hello", "body": "Everywhere"}
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(dispatch.call_args.kwargs["device_id"])

    def test_a_missing_field_is_refused(self) -> None:
        response, dispatch = self._send(f"{DIRECT}?dryRun=true", {"title": "no body"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "push_field_required")
        dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
