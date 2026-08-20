from __future__ import annotations

import re

# Shared across every marketplace-listing source (eBay, PriceCharting, and
# whatever comes next) -- a listing whose title matches one of these is
# almost never "this specific catalog item for sale," it's a lot, a bundle,
# a knockoff, or a pick-your-own multi-item listing. Same "no match beats a
# wrong match" discipline as every image-matching source in this codebase
# (see catalog_search_service.py's per-category enrichment comments): a
# missing listing is a non-event, a wrong one actively misleads someone
# about to spend real money.
#
# Deliberately general/category-agnostic (not "no controller" etc, which
# would be video-game-specific) so this applies uniformly whether the
# catalog item is a sports card, a video game, a LEGO set, or a Funko Pop
# -- category-specific rules can be layered on top later if a real false-
# positive pattern shows up, but this is the safe, broadly-applicable
# baseline confirmed against real eBay search results before shipping
# (a UPC-scoped "God of War" search still returned a bare PS4 console, a
# multi-game "pick and choice" lot, and a strategy-guide bundle).
_JUNK_LISTING_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\blot of\b",
        r"\bjob\s*lot\b",
        r"\bbundle\b",
        r"\bwholesale\b",
        r"\bpick\s*(?:and|&|,)?\s*choice\b",
        r"\byou\s*pick\b",
        r"\bchoose\s*your\b",
        r"\bassorted\b",
        r"\breplica\b",
        r"\breproduction\b",
        r"\bbootleg\b",
        r"\bcustom\s*made\b",
        r"\bfan\s*made\b",
        r"\bmany\s*titles\b",
        # A strategy guide/manual "w/ copy of game" bundle still overlaps
        # a game's title (it names the game on its cover) but isn't a
        # plain copy of the item -- live-confirmed real case: "GOD OF WAR:
        # Collector's Edition Strategy Guide (HC) w/ Copy of Game for PS4"
        # survived the junk-keyword + title-overlap filters otherwise.
        r"\bstrategy\s*guide\b",
        r"\bplayer'?s?\s*guide\b",
        r"\bprima\s*guide\b",
        r"\binstruction\s*manual\b",
    ]
]


def is_junk_listing_title(title: str) -> bool:
    if not title:
        return False
    return any(pattern.search(title) for pattern in _JUNK_LISTING_PATTERNS)


_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "and", "for", "with", "new", "used", "of", "in", "on", "to",
    "a", "an", "is", "at", "by", "or", "playstation", "nintendo", "xbox",
    "ps", "ps1", "ps2", "ps3", "ps4", "ps5", "switch", "game", "games",
    "card", "cards", "set",
}


def _significant_words(title: str) -> set[str]:
    return {
        word
        for word in _WORD_RE.findall(title.lower())
        if len(word) > 2 and word not in _STOPWORDS
    }


def has_meaningful_title_overlap(catalog_title: str, listing_title: str) -> bool:
    # Known residual gap, not fixed by this: a catalog title that reduces
    # to a single generic English word after stopword filtering (e.g.
    # "Brothers") can still one-word-match an unrelated listing that
    # happens to share it (live-confirmed: "Warner Brothers Hogwarts
    # Legacy" matched a "Brothers" search). Requiring multi-word overlap
    # doesn't help here since there's only one significant word to begin
    # with -- a real fix would need semantic matching, not keyword
    # overlap, and is out of scope for this pass. Narrower and rarer than
    # the console/lot/guide false positives this function does catch.
    # Requires at least one non-trivial word shared between the catalog
    # item's own title and a candidate listing's title -- catches results
    # that a UPC/GTIN or keyword search returned despite having nothing to
    # do with the actual item (live-confirmed real case: a UPC-scoped
    # "God of War" search on eBay returned a bare PS4 console listing with
    # zero title overlap at all). Deliberately loose (any one word, not
    # all) since listing titles are seller-written free text and rarely
    # match a catalog title's exact phrasing -- this only needs to reject
    # listings that share NOTHING with the item, not demand a full match.
    # Stopwords/platform names are excluded so two unrelated PS4 items
    # don't "match" purely because they're both on PS4.
    catalog_words = _significant_words(catalog_title)
    if not catalog_words:
        return True
    listing_words = _significant_words(listing_title)
    return bool(catalog_words & listing_words)
