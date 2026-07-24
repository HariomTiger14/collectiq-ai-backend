from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PricingCachePolicy:
    """TTL policy for shared valuation cache entries."""

    category: str
    valuation_status: str
    ttl_seconds: int
    reason: str


_FAST_MOVING_KEYWORDS = {
    "card",
    "trading card",
    "pokemon",
    "pokémon",
    "sports card",
    "sneaker",
    "shoe",
    "streetwear",
}

_STABLE_COLLECTIBLE_KEYWORDS = {
    "video game",
    "game",
    "comic",
    "lego",
    "funko",
    "coin",
}


def pricing_cache_policy(
    *,
    category: str | None,
    valuation_status: str | None,
) -> PricingCachePolicy:
    normalized_category = (category or "").strip().lower()
    normalized_status = (valuation_status or "").strip().lower()

    if normalized_status in {"provider_not_configured", "unavailable"}:
        return PricingCachePolicy(
            category=normalized_category,
            valuation_status=normalized_status or "unavailable",
            ttl_seconds=7 * 24 * 60 * 60,
            reason="Unavailable specialist/provider states are cached for 7 days.",
        )

    if normalized_status in {"lookup_failed"}:
        return PricingCachePolicy(
            category=normalized_category,
            valuation_status=normalized_status,
            ttl_seconds=60 * 60,
            reason="Temporary lookup failures are cached briefly.",
        )

    if any(keyword in normalized_category for keyword in _FAST_MOVING_KEYWORDS):
        return PricingCachePolicy(
            category=normalized_category,
            valuation_status=normalized_status or "market_estimated",
            ttl_seconds=24 * 60 * 60,
            reason="Fast-moving collectibles refresh every 24 hours.",
        )

    if any(keyword in normalized_category for keyword in _STABLE_COLLECTIBLE_KEYWORDS):
        return PricingCachePolicy(
            category=normalized_category,
            valuation_status=normalized_status or "market_estimated",
            ttl_seconds=72 * 60 * 60,
            reason="Stable collectible categories refresh every 72 hours.",
        )

    return PricingCachePolicy(
        category=normalized_category,
        valuation_status=normalized_status or "market_estimated",
        ttl_seconds=48 * 60 * 60,
        reason="General market values refresh every 48 hours.",
    )
