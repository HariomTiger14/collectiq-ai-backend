"""Test credentials for admin routes.

The static `ADMIN_IMPORT_TOKEN` used to hold every permission, so a test that
wanted to reach any admin route just set it and sent it. That is no longer
true: it now holds only `admin:read`, `audit:read` and `imports:run`, because
it is a shared operational secret rather than a person.

Tests whose subject is a route's behaviour -- not its authentication -- should
therefore authenticate as the identity that really performs the action: a
Supabase console admin. `console_admin()` does that. `static_import_token()`
is for the cases where the operational token genuinely is the caller, such as
the PriceCharting import runbook.

Both stub the auth layer rather than the transport, so a test never depends on
Supabase being reachable.
"""

from contextlib import contextmanager
from unittest.mock import patch

from app.routers.admin_auth import (
    FULL_ADMIN_PERMISSIONS,
    ROLE_PERMISSIONS,
    STATIC_IMPORT_TOKEN_PERMISSIONS,
)


# What tests send. Any value works for console_admin(): the point is that it
# does NOT match the configured static token, so resolution falls through to
# the Supabase profile path.
ADMIN_TOKEN = "secret-token"
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}
ADMIN_BEARER = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def console_admin_identity(role: str = "admin") -> dict:
    """The dict `_require_supabase_admin` returns for a real console admin."""
    permissions = sorted(ROLE_PERMISSIONS.get(role, FULL_ADMIN_PERMISSIONS))
    return {
        "id": "console-admin",
        "email": "admin@packlox.com",
        "role": role,
        "isAdmin": True,
        "permissions": permissions,
        "canWrite": bool(set(permissions) - {"admin:read", "audit:read"}),
    }


@contextmanager
def console_admin(role: str = "admin"):
    """Authenticate as a Supabase console admin for the duration of the block.

    The configured static token is deliberately set to something the test does
    not send, so the static branch cannot match and resolution reaches the
    profile-role path -- which is what a person signing into the console
    actually exercises.
    """
    with patch("app.routers.admin_auth.settings") as auth_settings, patch(
        "app.routers.admin_auth._supabase_admin_token",
        return_value=console_admin_identity(role),
    ):
        auth_settings.admin_import_token = "a-different-static-token"
        auth_settings.admin_job_token = "a-different-job-token"
        yield auth_settings


@contextmanager
def static_import_token():
    """Authenticate as the static ADMIN_IMPORT_TOKEN.

    Only `admin:read`, `audit:read` and `imports:run` -- the operational
    surface documented in the PriceCharting import runbook.
    """
    with patch("app.routers.admin_auth.settings") as auth_settings:
        auth_settings.admin_import_token = ADMIN_TOKEN
        yield auth_settings


@contextmanager
def static_job_token():
    """Authenticate as the static ADMIN_JOB_TOKEN, i.e. as a cron would."""
    with patch("app.routers.admin_auth.settings") as auth_settings:
        auth_settings.admin_job_token = ADMIN_TOKEN
        auth_settings.admin_import_token = "a-different-import-token"
        yield auth_settings


__all__ = [
    "ADMIN_TOKEN",
    "ADMIN_HEADERS",
    "ADMIN_BEARER",
    "console_admin",
    "console_admin_identity",
    "static_import_token",
    "static_job_token",
    "STATIC_IMPORT_TOKEN_PERMISSIONS",
]
