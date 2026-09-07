"""Where CSV downloads go, and what a CSV must prove before it is written.

Two rules live here because both were learned the same way -- by a silent
failure that looked exactly like success.

HOST POLICY
-----------
`/price-guide/download-custom` is served identically by pricecharting.com and
sportscardspro.com; the vendor confirmed this directly (JJ Hendricks,
2026-09-05: "The CSV download is the same either way, but CloudFlare might be
treating them differently"). Cloudflare does treat them differently:
sportscardspro.com returns a 403 "Just a moment..." challenge, first from
Render's IPs around 2026-08-31 and -- verified 2026-09-07 -- from a laptop
too. There is no longer any host from which the sportscardspro.com CSV path
works, so every CSV download now goes to pricecharting.com regardless of which
site a registry row came from.

Verified live 2026-09-07 against pricecharting.com with the correct
`console-uids` parameter:

    G96177 (video game) -> 200, 534 bytes, 1 row,   1 family
    G24631 (sports)     -> 200, 36,061 bytes, 300 rows, 1 family
                           ("Baseball Cards 1887 N172 Old Judge")

console_uid values are shared across both sites, which is why a sports uid
resolves correctly on the pricecharting.com host.

This deliberately does NOT change /api/products or /api/product routing.
Those are not blocked on sportscardspro.com (see the note in
backfill_pricecharting_sets.py) and are outside the CSV migration.

FAMILY VALIDATION
-----------------
download-custom does not validate its filter. Asking for `console_uids`
(underscore) instead of `console-uids` (hyphen) returns HTTP 200,
`text/csv`, 19.9 MB and 123,166 perfectly well-formed rows -- of the entire
video-game catalog, for a request that named three baseball sets. Nothing
downstream could tell: the columns parse, the prices are real, and the rows
would have landed under the tier-3 source tag with their sets stamped as
freshly refreshed.

So a CSV must prove it is the CSV that was asked for before any of it is
written. Two independent checks, because either alone has a blind spot:

  * count  -- N requested uids cannot yield more than N families. This is
    what catches the 123,166-row wrong-catalog dump (229 families for 3
    uids) even when the names are unrecognisable.
  * naming -- every returned family must correspond to a requested set.
    This catches a wrong response that happens to be small, which the count
    check alone would wave through.

Validation FAILS CLOSED: an unrecognised family aborts the batch before any
write, and the sets are not stamped refreshed, so the next run re-fetches
them. That is deliberate. If the vendor renames a console-name and this
starts aborting, the cost is a stalled batch and a loud error naming exactly
which family was unexpected -- recoverable. The cost of failing open is
writing the wrong catalog over the right one, which is not.
"""

from __future__ import annotations

import re

# Every CSV download goes here, whatever the registry row's source_site says.
CSV_DOWNLOAD_BASE_URL = "https://www.pricecharting.com"

# Statuses that mean "the endpoint is unwell", NOT "this set is bad".
#
# This distinction has teeth in the tier-3 rotation: a status that is neither
# 429 nor 403 is read as download-custom refusing a specific console_uid, and
# the batch is isolated one set at a time, with each individually-failing set
# recorded via record_tier3_failures(). Three such records park a set out of
# the rotation queue entirely. A transient 503 hitting a whole batch would
# therefore burn the isolation budget and could permanently park healthy
# sets -- so transient statuses must be handled as a batch-level failure and
# retried, never isolated.
TRANSIENT_CSV_STATUSES = frozenset({500, 502, 503, 504})


class CsvFamilyMismatch(Exception):
    """A CSV's contents do not correspond to the sets that were requested.

    Raised before any write. Carries both sides so the operator does not
    have to re-run anything to see what arrived.
    """

    def __init__(
        self,
        message: str,
        *,
        expected: list[str],
        observed: list[str],
        requested_uid_count: int,
    ) -> None:
        super().__init__(message)
        self.expected = expected
        self.observed = observed
        self.requested_uid_count = requested_uid_count


def csv_base_url(source_site: str | None = None) -> str:
    """Host for a /price-guide/download-custom call.

    Takes source_site so call sites read honestly -- they ARE routing a
    sportscardspro row -- while the answer stays the same for every site.
    Keeping the parameter also means the policy can be reversed here alone
    if Cloudflare's treatment of the two hosts ever changes back.
    """
    return CSV_DOWNLOAD_BASE_URL


def normalize_family(value: str | None) -> str:
    """Fold a console-name or set_name to a comparable form.

    Case and whitespace only. Punctuation is deliberately preserved: set
    names differ meaningfully by it ("Colgan's Chips"), and stripping it
    would make two distinct sets compare equal, which is the wrong error to
    make in a guard whose whole job is telling sets apart.
    """
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _matches_requested(observed: str, expected_normalized: list[str]) -> bool:
    """Does one returned console-name correspond to a requested set?

    The vendor returns a category-qualified name -- set_name "1887 N172 Old
    Judge" comes back as console-name "Baseball Cards 1887 N172 Old Judge"
    -- so equality alone is too strict. But bare containment is too loose:
    "Set 1" would match "Baseball Cards Set 10", and set names really do
    differ only by a trailing number ("2023 Panini Prizm 1" vs "... 10"),
    so that is a live way to accept the wrong set's data as the right set's.

    The prefix a category adds is always whole words, so requiring the match
    to fall on a word boundary keeps the real case working and closes the
    accident: "…cards set 10" does not end with " set 1".
    """
    for expected in expected_normalized:
        if not expected:
            continue
        if observed == expected:
            return True
        if observed.endswith(" " + expected) or expected.endswith(" " + observed):
            return True
    return False


def families_in_rows(raw_rows) -> set[str]:
    """Collect the console-name families present in parsed CSV rows.

    The vendor's header is `console-name`; some paths normalise it to
    `console_name`. Accepting both here means one place knows that, rather
    than each caller half-remembering it -- which is how a filter comes to
    look at a key that is never present and quietly validate nothing.
    """
    families: set[str] = set()
    for raw in raw_rows:
        name = raw.get("console-name") or raw.get("console_name")
        if name:
            families.add(name)
    return families


def mismatch_detail(exc: "CsvFamilyMismatch", *, limit: int = 5) -> dict:
    """The bit of a refusal worth keeping in a run summary.

    A refusal used to exist only as a stdout line, so from the ops ledger it
    looked like unexplained failed rows and diagnosing it meant going to
    Render's logs (§7 item 18j). Bounded on purpose: a wrong catalog can
    carry hundreds of families and the summary is not a log.
    """
    return {
        "requestedUidCount": exc.requested_uid_count,
        "expectedSetNames": exc.expected[:limit],
        "observedFamilies": exc.observed[:limit],
        "observedFamilyCount": len(exc.observed),
        "reason": str(exc)[:300],
    }


def validate_csv_families(
    observed_console_names: set[str] | list[str],
    *,
    expected_set_names: list[str],
    requested_uid_count: int,
) -> None:
    """Raise CsvFamilyMismatch unless the CSV matches what was requested.

    An empty CSV passes: there is nothing to write, and a legitimately empty
    set is not a wrong-catalog signal. The caller decides whether zero rows
    is worth reporting on its own terms.
    """
    observed = sorted({normalize_family(name) for name in observed_console_names if name})
    if not observed:
        return

    if requested_uid_count > 0 and len(observed) > requested_uid_count:
        raise CsvFamilyMismatch(
            f"CSV returned {len(observed)} console-name families for "
            f"{requested_uid_count} requested console_uid(s) -- a filtered "
            f"download cannot widen. Refusing to write. "
            f"First families seen: {observed[:5]}",
            expected=expected_set_names,
            observed=observed,
            requested_uid_count=requested_uid_count,
        )

    expected_normalized = [normalize_family(name) for name in expected_set_names]
    if not any(expected_normalized):
        # Nothing to compare against; the count check above is all we have.
        return

    unexpected = [
        name for name in observed if not _matches_requested(name, expected_normalized)
    ]
    if unexpected:
        raise CsvFamilyMismatch(
            f"CSV contains {len(unexpected)} console-name family/families that "
            f"match no requested set. Refusing to write. "
            f"Unexpected: {unexpected[:5]} | Requested: {expected_set_names[:5]}",
            expected=expected_set_names,
            observed=observed,
            requested_uid_count=requested_uid_count,
        )
