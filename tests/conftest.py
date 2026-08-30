"""Makes the test process hermetic: a developer's machine configuration must
never change what the backend suite does.

The problem this fixes, measured 2026-08-31 on clean `main`:

    with a developer .env present :  47 failed, 973 passed  (~35s)
    with .env neutralised         :   0 failed, 1020 passed  (~4.6s)

Every one of those failures was contamination, not a product defect. Two
root causes, both traced by loading .env with individual variables withheld:

  * SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY -- 41 failures. Tests exercise
    the API through TestClient, so with real credentials present the service
    layer stopped using its fixtures and talked to PRODUCTION Supabase. The
    eightfold runtime difference above is almost entirely those network
    calls. This is the same class of bug as the ops-ledger pollution fixed in
    app/services/ops/observability.py, where a test run wrote rows into the
    production run ledger.
  * PRICECHARTING_API_KEY -- 6 failures. Its presence flips code onto the
    "provider configured" branch, so tests asserting the unconfigured path
    saw a live provider instead.

app/core/config.py calls load_dotenv() at import with an absolute path, so
neither cwd nor a cleaned shell environment prevents this -- and Settings
reads os.environ when the class is DEFINED, so anything we do has to happen
before app.core.config is first imported. A conftest at the tests root is
exactly that moment: pytest imports it before collecting test modules.

Fixing it here rather than in app/core/config.py is deliberate: production
must keep loading .env exactly as it does today. Nothing in this file
changes application behaviour; it only controls what the test process
inherits.

tests/test_environment_hermeticity.py asserts the result, so if this
isolation is ever weakened the suite says so in one obvious failure instead
of ~47 confusing ones.
"""

import os
import re

# Names cleared before the application is imported. Two shapes:
#   * exact names this backend reads for infrastructure it could reach
#   * patterns covering third-party credentials, since a configured
#     provider changes which branch the code under test takes
_EXACT = frozenset(
    {
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_ANON_KEY",
        "DATABASE_URL",
        "ADMIN_JOB_TOKEN",
    }
)
_PATTERNS = (
    re.compile(r"_API_KEY$"),
    re.compile(r"^EBAY_"),
    re.compile(r"^OPENAI_"),
    re.compile(r"^GEMINI_"),
    re.compile(r"^PRICECHARTING_"),
)


def _should_clear(name: str) -> bool:
    return name in _EXACT or any(pattern.search(name) for pattern in _PATTERNS)


def _isolate() -> None:
    # Neutralise .env before app.core.config can load it. Stubbing the
    # function is what makes this work regardless of the path config passes.
    try:
        import dotenv

        dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]
    except ImportError:  # pragma: no cover -- dotenv is a hard dependency
        pass

    # A developer (or CI) may also export these directly, which .env
    # stubbing alone would not catch.
    for name in [name for name in os.environ if _should_clear(name)]:
        del os.environ[name]

    # Deliberately NOT setting OPS_OBSERVABILITY_DISABLED here. The existing
    # PYTEST_CURRENT_TEST guard in app/services/ops/observability.py already
    # keeps observability writes off for the whole suite, and pinning the
    # override on would break the test that proves that guard LIFTS outside
    # tests -- buying no real safety at the cost of a genuine assertion.


_isolate()
