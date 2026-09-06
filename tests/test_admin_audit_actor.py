"""Audit events must name whoever actually performed the action.

`AdminAuditService.record` defaults `actor="admin_token"`, so a call site that
simply omitted the argument did not fail -- it silently mislabelled. Six
routers omitted it, and their audit helpers did not even accept an admin, so
every pricing override, scan resolution, catalog edit, portfolio edit, report
export and admin note was attributed to the machine token no matter who was
signed in. Observed live: a catalog edit made as admin@packlox.com recorded
`admin_token`, while an admin_users event 27 seconds earlier in the same
session recorded the real email -- because admin_users.py was the one router
that had been fixed (PR #89).

The identity was always reachable. Most handlers annotated the dependency as
`None` while it returned a dict, so nothing about the code suggested there was
anything to pass.

`admin_token` remains correct for a runbook or cron: `_static_admin()` returns
`id="admin_token"` with an empty email, so `email or id` resolves to it
without branching. Those cases are asserted here too -- recording a real
machine action as a machine is the other half of being right.

No historical rows are rewritten: they record what was known when written.
"""

import unittest
from typing import Any
from unittest.mock import patch

from app.routers.admin_auth import FULL_ADMIN_PERMISSIONS, STATIC_IMPORT_TOKEN_PERMISSIONS, _static_admin


CONSOLE_ADMIN = {
    "id": "console-admin-id",
    "email": "admin@packlox.com",
    "role": "admin",
    "isAdmin": True,
    "permissions": sorted(FULL_ADMIN_PERMISSIONS),
    "canWrite": True,
}

# Every router whose audit helper had to start accepting an admin.
ROUTERS = (
    "admin_pricing",
    "admin_scans",
    "admin_catalog",
    "admin_portfolio",
    "admin_reports",
    "admin_notes",
)


def _record_audit_for(module_name: str):
    module = __import__(f"app.routers.{module_name}", fromlist=["_record_audit"])
    return module, module._record_audit


def _call_helper(module_name: str, admin: dict[str, Any] | None):
    """Invoke a router's audit helper and return what reached the service."""
    module, helper = _record_audit_for(module_name)
    with patch.object(module, "AdminAuditService") as service:
        # Each helper has its own positional/keyword shape; supply the minimum.
        if module_name in ("admin_pricing", "admin_scans"):
            helper(action="a", status="success", admin=admin)
        elif module_name == "admin_portfolio":
            helper(action="a", status="success", admin=admin)
        elif module_name == "admin_catalog":
            helper("a", "success", "target-1", {}, admin=admin)
        elif module_name == "admin_notes":
            helper("a", "success", "user", "target-1", {}, admin=admin)
        elif module_name == "admin_reports":
            helper("a", "success", {}, admin=admin)
        else:  # pragma: no cover - guard against a new router being added blind
            raise AssertionError(f"no call shape defined for {module_name}")
        service.return_value.record.assert_called_once()
        return service.return_value.record.call_args.kwargs


class ConsoleAdminIsNamedTest(unittest.TestCase):
    """A signed-in admin must appear in the log by email."""

    def test_every_router_records_the_console_admin_email(self) -> None:
        for module_name in ROUTERS:
            with self.subTest(router=module_name):
                kwargs = _call_helper(module_name, CONSOLE_ADMIN)
                self.assertEqual(kwargs["actor"], "admin@packlox.com")

    def test_an_admin_without_an_email_falls_back_to_its_id(self) -> None:
        """Same precedence as admin_users.py: email, then id."""
        admin = {**CONSOLE_ADMIN, "email": ""}
        for module_name in ROUTERS:
            with self.subTest(router=module_name):
                kwargs = _call_helper(module_name, admin)
                self.assertEqual(kwargs["actor"], "console-admin-id")


class MachineIdentityStaysMachineTest(unittest.TestCase):
    """Recording a real machine action as a machine is the other half."""

    def test_static_token_identity_still_records_admin_token(self) -> None:
        static = _static_admin(STATIC_IMPORT_TOKEN_PERMISSIONS)
        self.assertEqual(static["id"], "admin_token")
        self.assertEqual(static["email"], "")
        for module_name in ROUTERS:
            with self.subTest(router=module_name):
                kwargs = _call_helper(module_name, static)
                self.assertEqual(kwargs["actor"], "admin_token")

    def test_job_token_identity_still_records_admin_token(self) -> None:
        job = _static_admin(FULL_ADMIN_PERMISSIONS)
        for module_name in ROUTERS:
            with self.subTest(router=module_name):
                self.assertEqual(_call_helper(module_name, job)["actor"], "admin_token")

    def test_a_missing_admin_degrades_to_admin_token_rather_than_raising(self) -> None:
        """A call site missed in a future change must not break the action.

        The parameter defaults to None precisely so an omission degrades to
        today's behaviour instead of raising inside an audit write.
        """
        for module_name in ROUTERS:
            with self.subTest(router=module_name):
                self.assertEqual(_call_helper(module_name, None)["actor"], "admin_token")


class AuditRecordingStaysBestEffortTest(unittest.TestCase):
    def test_a_failing_audit_service_never_breaks_the_action(self) -> None:
        for module_name in ROUTERS:
            with self.subTest(router=module_name):
                module, helper = _record_audit_for(module_name)
                with patch.object(module, "AdminAuditService", side_effect=RuntimeError("audit down")):
                    # Must not raise.
                    if module_name in ("admin_pricing", "admin_scans", "admin_portfolio"):
                        helper(action="a", status="success", admin=CONSOLE_ADMIN)
                    elif module_name == "admin_catalog":
                        helper("a", "success", "t", {}, admin=CONSOLE_ADMIN)
                    elif module_name == "admin_notes":
                        helper("a", "success", "user", "t", {}, admin=CONSOLE_ADMIN)
                    else:
                        helper("a", "success", {}, admin=CONSOLE_ADMIN)


class NoHandlerLiesAboutItsDependencyTest(unittest.TestCase):
    """The annotation that hid the identity in the first place.

    A handler declaring `_admin: None` while the dependency returns a dict is
    what made the actor look unavailable. This fails if a new route
    reintroduces it, which is the cheapest way to stop this recurring.
    """

    def test_no_route_annotates_an_admin_dependency_as_none(self) -> None:
        import pathlib

        offenders = []
        for path in sorted(pathlib.Path("app/routers").glob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if "_admin: None = Depends(" in line:
                    offenders.append(f"{path.name}:{number}")

        self.assertEqual(
            offenders,
            [],
            "admin dependencies return a dict with the resolved identity; "
            "annotating them as None hides it from the handler: " + ", ".join(offenders),
        )


class AuditHelpersCanNameWhoActedTest(unittest.TestCase):
    """Catches a new router copying the old, identity-blind helper shape.

    Two shapes are legitimate and both are accepted: taking the admin dict
    and resolving inside (most routers), or taking an already-resolved
    `actor` string and resolving at the call site
    (admin_catalog_image_flags). What is NOT acceptable is a helper that
    cannot express who acted at all -- that is the defect this task fixed,
    and it is invisible at the call site because AdminAuditService.record
    defaults the actor rather than requiring it.
    """

    def test_every_router_audit_helper_can_name_the_actor(self) -> None:
        import inspect

        for module_name in ROUTERS + ("admin_users", "admin_catalog_image_flags"):
            with self.subTest(router=module_name):
                module = __import__(
                    f"app.routers.{module_name}", fromlist=["_record_audit"]
                )
                helper = getattr(module, "_record_audit", None)
                self.assertIsNotNone(helper, f"{module_name} has no _record_audit")
                params = set(inspect.signature(helper).parameters)
                self.assertTrue(
                    params & {"admin", "actor"},
                    f"{module_name}._record_audit cannot name who acted -- it takes "
                    "neither an admin dict nor a resolved actor",
                )


if __name__ == "__main__":
    unittest.main()
