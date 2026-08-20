import unittest

from app.services.pricing.marketplace_listing_filters import (
    has_meaningful_title_overlap,
    is_junk_listing_title,
)


class IsJunkListingTitleTest(unittest.TestCase):
    def test_flags_lots_and_bundles(self) -> None:
        junk_titles = [
            "PlayStation 4 Games - PS4 - Many Titles Pick And Choice",
            "Lot of 10 Pokemon Cards",
            "Job lot of retro games",
            "Xbox 360 Bundle with 5 games",
            "Wholesale lot 20x sports cards",
            "You Pick Pokemon Card Bulk",
            "Choose Your Card - Pokemon Base Set",
            "Assorted Funko Pop Lot",
            "Replica Championship Belt",
            "Reproduction Vintage Card",
            "Bootleg NES Cartridge",
        ]
        for title in junk_titles:
            self.assertTrue(is_junk_listing_title(title), title)

    def test_flags_guide_and_manual_bundles(self) -> None:
        self.assertTrue(
            is_junk_listing_title(
                "GOD OF WAR: Collector's Edition Strategy Guide (HC) w/ Copy of Game for PS4"
            )
        )
        self.assertTrue(is_junk_listing_title("Zelda Player's Guide"))
        self.assertTrue(is_junk_listing_title("Halo 3 Instruction Manual only"))

    def test_does_not_flag_real_listings(self) -> None:
        real_titles = [
            "God Of War - Sony Playstation 4 PS4 Pristine Tested 1Y Guarantee",
            "God of War Ragnarok - PlayStation 4 - Brand NEW - Factory Sealed",
            "2013 POKEMON HANAFUDA CHARIZARD KANTO-DECEMBER",
            "Brothers: A Tale of Two Sons (Sony PlayStation 4, 2015)",
        ]
        for title in real_titles:
            self.assertFalse(is_junk_listing_title(title), title)

    def test_empty_title_is_not_junk(self) -> None:
        self.assertFalse(is_junk_listing_title(""))


class HasMeaningfulTitleOverlapTest(unittest.TestCase):
    def test_rejects_listing_with_no_shared_words(self) -> None:
        self.assertFalse(
            has_meaningful_title_overlap(
                "God of War Playstation 4",
                "PS4 1TB BLACK CONSOLE  SYSTEM -BLACK  12 LBS - GOOD -slightly used",
            )
        )

    def test_accepts_listing_sharing_a_significant_word(self) -> None:
        self.assertTrue(
            has_meaningful_title_overlap(
                "God of War Playstation 4",
                "God of War 2018 - Sony PlayStation 4 PS4 - Tested Pristine",
            )
        )

    def test_ignores_platform_and_stopwords_when_matching(self) -> None:
        # Two different PS4 games should NOT "match" purely because both
        # titles mention the platform -- only real content words count.
        self.assertFalse(
            has_meaningful_title_overlap(
                "Brothers Playstation 4",
                "Some Unrelated Game for the Playstation 4 Console",
            )
        )

    def test_no_significant_words_in_catalog_title_defaults_to_accept(self) -> None:
        # A catalog title that's entirely stopwords/platform names has
        # nothing to check against -- fail open rather than reject
        # everything.
        self.assertTrue(has_meaningful_title_overlap("Playstation 4", "Anything at all"))


if __name__ == "__main__":
    unittest.main()
