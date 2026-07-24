import unittest

from app.services.pricing.cache_policy import pricing_cache_policy


class PricingCachePolicyTest(unittest.TestCase):
    def test_fast_moving_collectibles_refresh_daily(self) -> None:
        policy = pricing_cache_policy(
            category="Trading Card",
            valuation_status="market_estimated",
        )

        self.assertEqual(policy.ttl_seconds, 24 * 60 * 60)

    def test_stable_collectibles_refresh_every_three_days(self) -> None:
        policy = pricing_cache_policy(
            category="LEGO",
            valuation_status="market_estimated",
        )

        self.assertEqual(policy.ttl_seconds, 72 * 60 * 60)

    def test_unavailable_provider_state_is_cached_for_a_week(self) -> None:
        policy = pricing_cache_policy(
            category="Luxury Watch",
            valuation_status="provider_not_configured",
        )

        self.assertEqual(policy.ttl_seconds, 7 * 24 * 60 * 60)

    def test_lookup_failures_are_short_lived(self) -> None:
        policy = pricing_cache_policy(
            category="Comic",
            valuation_status="lookup_failed",
        )

        self.assertEqual(policy.ttl_seconds, 60 * 60)


if __name__ == "__main__":
    unittest.main()
