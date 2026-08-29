"""Deterministic set resolution between PriceCharting Pokemon console names
and TCGdex sets.

Design principle (reviewer-approved 2026-08-29): no fuzzy multilingual or
runtime guessing. English sets resolve by normalized-name equality plus a
small explicit alias table; PriceCharting's single "Pokemon Promo" bucket
routes to TCGdex's per-era Black Star Promo sets purely off the card
number's era prefix; Japanese sets resolve only through the hand-verified
map below (unmapped means no attempt).
"""

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_set_key(name: str) -> str:
    """Lowercase, strip punctuation, collapse spaces. The import script
    stores this for every TCGdex set; the enrichment computes the same key
    from PriceCharting console names so lookup is a plain eq filter."""
    return " ".join(_NON_ALNUM.split((name or "").lower())).strip()


def normalize_card_number(number: str) -> str:
    n = (number or "").strip().lower().lstrip("0")
    return n or "0"


# PriceCharting console_name (lowercased, "pokemon " prefix stripped,
# normalized) -> TCGdex English set_key, for the cases where the two names
# genuinely differ. Everything else matches by identical normalized name.
_EN_ALIASES: dict[str, str] = {
    # PriceCharting says "Scarlet & Violet 151", TCGdex says "151".
    "scarlet violet 151": "151",
}

# "Pokemon Promo" era routing: PriceCharting numbers its promo rows with
# the era prefix baked into the card number (e.g. "#SM210", "#SWSH039",
# "#XY67"), which maps one-to-one onto TCGdex's Black Star Promo sets.
_PROMO_PREFIX_TO_SET_KEY: list[tuple[str, str]] = [
    ("swsh", "swsh black star promos"),
    ("svp", "svp black star promos"),
    ("sm", "sm black star promos"),
    ("xy", "xy black star promos"),
    ("bw", "bw black star promos"),
    ("dp", "dp black star promos"),
]

# Hand-verified English transliteration -> exact TCGdex Japanese set_name.
# Every entry was located in the live /v2/ja set list before being added
# (2026-08-29 audit: 11/12 candidates found; the mapping demo matched
# 31/31 card numbers inside these sets). Extend this table only with
# verified pairs -- an unmapped Japanese set simply gets no image attempt.
JA_SET_MAP: dict[str, str] = {
    "clay burst": "クレイバースト",
    "cyber judge": "サイバージャッジ",
    "raging surf": "レイジングサーフ",
    "wild force": "ワイルドフォース",
    "shiny treasure ex": "シャイニートレジャーex",
    "black bolt": "ブラックボルト",
    "white flare": "ホワイトフレア",
    "stellar miracle": "ステラミラクル",
    "crimson haze": "クリムゾンヘイズ",
    "night wanderer": "ナイトワンダラー",
    "paradise dragona": "楽園ドラゴーナ",
    "battle partners": "バトルパートナーズ",
}


# Stage-1 safe same-face variant tags (2026-08-29 audit + reviewer
# approval): for these bracket tags the canonical TCGdex image depicts
# exactly the right card face -- the tag describes foil treatment or
# format, not a visible print difference. Each family was validated
# against real production rows with visual inspection before being
# listed. Everything NOT listed stays default-deny (placeholder); the
# list only grows by explicit review (stage-2 candidates: cosmos holo,
# cracked ice, rainbow foil, sparkle/spectra/tekno, prism, ...).
SAFE_VARIANT_TAGS: frozenset[str] = frozenset({
    "reverse holo",
    "reverse",
    "holo",
    "jumbo",
})


def is_safe_variant_tag(variant_token: str | None) -> bool:
    if not variant_token:
        return False
    return variant_token.strip().lower() in SAFE_VARIANT_TAGS


def resolve_english_set_key(console_name: str) -> str | None:
    """PriceCharting console_name ("Pokemon Stellar Crown") -> TCGdex
    English set_key ("stellar crown"), or None when this console is not an
    English expansion (Japanese and promo consoles resolve elsewhere)."""
    lowered = (console_name or "").strip().lower()
    if not lowered.startswith("pokemon"):
        return None
    if "japanese" in lowered or lowered == "pokemon promo":
        return None
    key = normalize_set_key(lowered.removeprefix("pokemon").strip())
    if not key:
        return None
    return _EN_ALIASES.get(key, key)


def resolve_promo_set_key(card_number: str) -> str | None:
    """"Pokemon Promo" rows: route by the era prefix of the card number
    ("SWSH039" -> SWSH Black Star Promos). No prefix, no attempt."""
    lowered = (card_number or "").strip().lower()
    for prefix, set_key in _PROMO_PREFIX_TO_SET_KEY:
        if lowered.startswith(prefix):
            return set_key
    return None


def resolve_japanese_set_name(console_name: str) -> str | None:
    """"Pokemon Japanese Clay Burst" -> exact TCGdex set_name, via the
    hand-verified map only."""
    lowered = (console_name or "").strip().lower()
    if not lowered.startswith("pokemon japanese"):
        return None
    key = normalize_set_key(lowered.removeprefix("pokemon japanese").strip())
    return JA_SET_MAP.get(key)
