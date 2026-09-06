"""Redaction applied to anything written into the ops tables.

Deliberately dependency-free -- only `re`. Cron scripts import this, and they
must keep running when the app's config, Supabase or env are unavailable
(see the design constraints at the top of scripts/_ops_run_recorder.py).
Importing it pulls in `app`, `app.services` and `app.services.ops`, all of
which are empty package markers, so nothing is loaded transitively.

It lived in scripts/_ops_run_recorder.py and covered the cron path only, so
API errors were reaching ops_error_events unredacted. One copy, both paths.
"""

import re


# Matches on the PARAMETER NAME rather than on known secret values, so it also
# covers credentials this module never sees. The leak that prompted it was a
# PriceCharting `?t=<token>` URL appearing in a traceback: _redact_token() in
# backfill_pricecharting_sets.py covered only print() calls in that one file
# and needed the token passed in, so every other job leaked by default.
_SECRET_QUERY_PARAMS = re.compile(
    r"([?&](?:t|key|token|api[-_]?key|apikey|access[-_]?token|password|secret)=)"
    r"""[^&\s"'>]+""",
    re.IGNORECASE,
)

# Bearer/apikey values in header dumps, which appear in httpx tracebacks and
# in provider error bodies. The query-parameter pattern above does not see
# these because they are not in a URL.
_BEARER_TOKENS = re.compile(
    r"((?:authorization|apikey|x-admin-token)['\"]?\s*[:=]\s*['\"]?)"
    r"(?:bearer\s+)?[A-Za-z0-9._\-]{12,}",
    re.IGNORECASE,
)


def scrub_secrets(text: str) -> str:
    """Redact credential-bearing query parameters and header values.

    Best-effort by design: it reduces what a leak exposes, it does not
    guarantee a value never appears. The stronger protection is not putting
    request bodies or provider payloads into the ledger in the first place.
    """
    if not text:
        return text
    scrubbed = _SECRET_QUERY_PARAMS.sub(r"\1[REDACTED]", text)
    return _BEARER_TOKENS.sub(r"\1[REDACTED]", scrubbed)
