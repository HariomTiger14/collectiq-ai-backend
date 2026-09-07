"""Whether a set can be refreshed through /api/products at all.

small-sets-refresh refreshes by TEXT SEARCH. That only works when the search
returns one complete, unambiguous set under the vendor's hard 100-result cap.
Some sets can never satisfy that, and the job re-discovers this every hour:
one API call, one 1.2s pace wait and one alarming log line per set per run,
indefinitely.

Verified live 2026-09-07 -- searching "2023 Panini Certified 2023" returned
exactly 100 products whose console-name was "Football Cards 2023 Panini
Select". Wrong set, truncated at the cap, and it would have been written as a
refresh of Panini Certified had the cap check not rejected it first.

TWO CLASSES OF MISS, TREATED DIFFERENTLY
----------------------------------------
Deterministic -- CAPPED_100 and WRONG_FAMILY. Tomorrow's answer is today's
answer: a set over the cap stays over it, and a name that resolves to another
set keeps resolving there. Mark ineligible immediately; waiting for three
identical results spends three days of API calls to learn nothing.

Transient-capable -- API_404 and API_EMPTY. A vendor blip, a deploy, a
momentary index gap all look like this, and marking on the first one would
quietly exclude good sets for a month. These require MISS_THRESHOLD
consecutive misses, and any success resets the counter.

That asymmetry is the whole design. The alternative -- one rule for
everything -- is either too eager (excludes good sets on a blip) or too slow
(keeps paying for sets that can never work).

NOT A FAILURE MARKER
--------------------
Tier-1 ineligible means "text search cannot serve this set". Tier-3 fetches
by console_uid + CSV and does not care about the search cap, so these sets
remain fully tier-3 eligible. Nothing here writes tier3_* columns, and a
30-day recheck exists because vendor catalogs change: a set that is 140 items
today may be searchable later, and a wrong fuzzy match can be fixed upstream.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

# The vendor's /api/products cap, confirmed live at exactly 100 results
# regardless of page/offset/cursor/limit (see backfill_pricecharting_sets).
API_SEARCH_RESULT_CAP = 100

# Consecutive transient-capable misses before a set is set aside.
MISS_THRESHOLD = 3

# How long a set stays out before tier-1 tries again.
RECHECK_DAYS = 30

REASON_404 = "api_404"
REASON_EMPTY = "api_empty"
REASON_CAPPED = "api_capped_100"
REASON_WRONG_FAMILY = "api_ambiguous_or_wrong_family"

OK = "ok"

# Reasons whose answer will not change tomorrow.
DETERMINISTIC_REASONS = frozenset({REASON_CAPPED, REASON_WRONG_FAMILY})
TRANSIENT_REASONS = frozenset({REASON_404, REASON_EMPTY})


def _normalize(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _same_family(console_name: str | None, set_name: str | None) -> bool:
    """Does a returned product belong to the set we searched for?

    Same word-boundary rule as the CSV family guard: the vendor returns a
    category-qualified name ("Football Cards 2023 Panini Select" for set name
    "2023 Panini Select"), so containment is right and equality is not -- but
    bare containment would let "2023 Panini Select" match
    "2023 Panini Select Draft", which is a different set.
    """
    observed, expected = _normalize(console_name), _normalize(set_name)
    if not observed or not expected:
        return False
    return (
        observed == expected
        or observed.endswith(" " + expected)
        or expected.endswith(" " + observed)
    )


def classify_search_result(
    products: list[dict[str, Any]] | None,
    *,
    set_name: str | None,
    http_status: int | None = None,
) -> str:
    """Classify one /api/products outcome. Returns OK or a reason constant.

    `products is None` means the call itself failed; http_status separates a
    genuine 404 from any other transport error, which is reported as a 404-
    class miss because both are "the search gave us nothing usable" and both
    are transient-capable.
    """
    if products is None:
        return REASON_404
    if len(products) == 0:
        return REASON_EMPTY
    if len(products) >= API_SEARCH_RESULT_CAP:
        # At or over the cap the result is truncated, so even a correct-looking
        # set is incomplete -- writing it would silently drop the tail.
        return REASON_CAPPED
    if set_name and not any(
        _same_family(product.get("console-name") or product.get("console_name"), set_name)
        for product in products
    ):
        # EVERY product belongs to some other set. A partial overlap is normal
        # for fuzzy search and is handled downstream by dedupe; a total miss
        # means the query resolves somewhere else entirely.
        return REASON_WRONG_FAMILY
    return OK


def plan_registry_update(
    reason: str,
    *,
    current_miss_count: int = 0,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The registry patch for one outcome, or None if there is nothing to write.

    Deliberately returns ONLY tier1_* fields. Tier-3 eligibility, failure
    counts and refresh timestamps are not this function's business, and a test
    asserts no tier3_ key can appear here.
    """
    moment = now or datetime.now(timezone.utc)

    if reason == OK:
        # Success clears both the counter and any standing exclusion: a set
        # that works now should not stay excluded because of an old blip.
        if current_miss_count == 0:
            return None
        return {
            "tier1_miss_count": 0,
            "tier1_refresh_eligible": True,
            "tier1_ineligible_reason": None,
            "tier1_ineligible_at": None,
            "tier1_recheck_after": None,
        }

    if reason in DETERMINISTIC_REASONS:
        return {
            "tier1_refresh_eligible": False,
            "tier1_ineligible_reason": reason,
            "tier1_ineligible_at": moment.isoformat(),
            "tier1_recheck_after": (moment + timedelta(days=RECHECK_DAYS)).isoformat(),
        }

    if reason in TRANSIENT_REASONS:
        misses = current_miss_count + 1
        if misses < MISS_THRESHOLD:
            # Counting, not excluding -- the set stays in the queue.
            return {"tier1_miss_count": misses}
        return {
            "tier1_miss_count": misses,
            "tier1_refresh_eligible": False,
            "tier1_ineligible_reason": reason,
            "tier1_ineligible_at": moment.isoformat(),
            "tier1_recheck_after": (moment + timedelta(days=RECHECK_DAYS)).isoformat(),
        }

    return None
