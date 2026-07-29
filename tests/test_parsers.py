from __future__ import annotations

import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from shadow_slave_monitor.config import PUBLIC_SITES, SourceConfig
from shadow_slave_monitor.parsers import ParseError, check_public_site, parse_telegram_candidates, parse_telegram_telegra_link
from shadow_slave_monitor.state_manager import validate_source_config


class NovelBuddyParserTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "Novel Buddy")

    def check(self, html: str):
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html):
            return check_public_site(self.source)

    def test_configuration_uses_only_me_domain(self) -> None:
        self.assertEqual(self.source.url, "https://novelbuddy.me/shadow-slave")
        self.assertEqual(self.source.allowed_hosts, ("novelbuddy.me", "www.novelbuddy.me"))
        self.assertNotIn("novelbuddy.io", repr(PUBLIC_SITES))

    def test_current_short_row_has_canonical_absolute_url_and_no_invented_title(self) -> None:
        report = self.check('<a href="/shadow-slave/chapter-3110-gazing-into-the-abyss">Ch. 3110</a>')
        self.assertEqual((report.chapter, report.title, report.url),
                         (3110, None, "https://novelbuddy.me/shadow-slave/chapter-3110-gazing-into-the-abyss"))

    def test_title_is_cleaned_without_losing_legitimate_number(self) -> None:
        report = self.check('<a href="/shadow-slave/chapter-3110-gazing-into-the-abyss">Chapter 3110: Gazing 2 Into the Abyss 2 hours ago</a>')
        self.assertEqual(report.title, "Gazing 2 Into the Abyss")

    def test_untrustworthy_links_and_counts_are_rejected(self) -> None:
        invalid = (
            '<p>3110 Chapters</p><p>55 reviews</p><a href="/shadow-slave/chapter-3109-title">Ch. 3110</a>',
            '<a href="/other/chapter-3110-title">Ch. 3110</a>',
            '<a href="https://evil.example/shadow-slave/chapter-3110-title">Ch. 3110</a>',
            '<a href="/shadow-slave/chapter-3110-title?token=secret">Ch. 3110</a>',
            '<a href="/shadow-slave/chapter-3110-title#comments">Ch. 3110</a>',
        )
        for html in invalid:
            with self.subTest(html=html), self.assertRaises(ParseError):
                self.check(html)


class NovelFireParserTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "NovelFire")

    def check(self, html: str):
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html):
            return check_public_site(self.source)

    def test_enabled_configuration_and_main_latest_card(self) -> None:
        validate_source_config()
        self.assertTrue(self.source.enabled)
        html = '<p>3122Chapters</p><a href="/book/shadow-slave/chapters">Novel Chapters Chapter 3122 Field of Dishonor Updated 3 hours ago</a>'
        report = self.check(html)
        self.assertEqual((report.chapter, report.title, report.url),
                         (3122, "Field of Dishonor", "https://novelfire.net/book/shadow-slave/chapters"))

    def test_latest_release_individual_url_and_numeric_title(self) -> None:
        report = self.check('<a href="/book/shadow-slave/chapter-3098">Latest Release: Chapter 3098 Catch 22 Updated 2 hours ago</a>')
        self.assertEqual((report.chapter, report.title, report.url),
                         (3098, "Catch 22", "https://novelfire.net/book/shadow-slave/chapter-3098"))

    def test_count_unrelated_book_and_mismatch_are_rejected(self) -> None:
        for html in ('<p>3122Chapters</p>',
                     '<a href="/book/other/chapter-3122">Chapter 3122 Wrong</a>',
                     '<a href="/book/shadow-slave/chapter-3121">Chapter 3122 Wrong</a>'):
            with self.subTest(html=html), self.assertRaises(ParseError):
                self.check(html)

    def test_conflict_keeps_higher_self_consistent_main_candidate(self) -> None:
        html = '''
          <a href="/book/shadow-slave/chapters">Novel Chapters Chapter 3122 Field of Dishonor Updated 3 hours ago</a>
          <a href="/book/shadow-slave/chapter-3098">Latest Release: Chapter 3098 Usurpation</a>
        '''
        with self.assertLogs(level="WARNING") as logs:
            report = self.check(html)
        self.assertEqual((report.chapter, report.title, report.url),
                         (3122, "Field of Dishonor", "https://novelfire.net/book/shadow-slave/chapters"))
        self.assertIn("NovelFire pages disagreed: chapters=3098,3122", "\n".join(logs.output))


class NovelArrowParserTests(unittest.TestCase):
    source = SourceConfig(
        "NovelArrow",
        "https://novelarrow.com/novel/shadow-slave",
        True,
        ("novelarrow.com", "www.novelarrow.com"),
    )

    def check(self, html: str):
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html):
            return check_public_site(self.source)

    def test_source_is_enabled_in_exact_configuration_after_novel_buddy(self) -> None:
        validate_source_config()
        sites = list(PUBLIC_SITES)
        index = next(i for i, site in enumerate(sites) if site.name == "NovelArrow")
        self.assertEqual(sites[index - 1].name, "Novel Buddy")
        self.assertTrue(sites[index].enabled)
        self.assertEqual(sites[index].allowed_hosts, ("novelarrow.com", "www.novelarrow.com"))

    def test_realistic_latest_chapter_section(self) -> None:
        html = """
            <main>
              <h1>Shadow Slave</h1>
              <p>3128 Chapters</p>
              <section>
                <h2>Latest chapter</h2>
                <a href="/chapter/shadow-slave/chapter-3128-song-of-fire">C3128 Song of Fire</a>
              </section>
              <section>
                <h2>Older chapters</h2>
                <a href="/chapter/shadow-slave/chapter-3127-burning-skies">C3127 Burning Skies</a>
                <a href="/chapter/shadow-slave/chapter-30-starless-void">C30 Starless Void 2</a>
              </section>
              <nav>Previous Next</nav><p>4.8 rating · 25 comments · updated 2 hours ago</p>
              <a href="/chapter/other-novel/chapter-9999-not-shadow-slave">C9999 Not Shadow Slave</a>
            </main>
        """
        report = self.check(html)
        self.assertEqual(
            (report.source, report.chapter, report.title, report.url),
            (
                "NovelArrow",
                3128,
                "Song of Fire",
                "https://novelarrow.com/chapter/shadow-slave/chapter-3128-song-of-fire",
            ),
        )

    def test_novel_wide_chapter_count_is_not_a_candidate(self) -> None:
        with self.assertRaises(ParseError):
            self.check("<main><h1>Shadow Slave</h1><p>3128 Chapters</p><p>Latest chapter</p></main>")

    def test_mismatched_visible_and_href_chapters_are_rejected(self) -> None:
        with self.assertRaises(ParseError):
            self.check(
                '<p>Latest chapter</p><a href="/chapter/shadow-slave/chapter-3128-song-of-fire">'
                "C3127 Song of Fire</a>"
            )

    def test_unrelated_novel_link_is_rejected(self) -> None:
        with self.assertRaises(ParseError):
            self.check(
                '<p>Latest chapter</p><a href="/chapter/other-novel/chapter-3128-song-of-fire">'
                "C3128 Song of Fire</a>"
            )

    def test_trailing_site_metadata_is_removed_when_slug_confirms_it(self) -> None:
        report = self.check(
            '<a href="/chapter/shadow-slave/chapter-30-starless-void">C30 Starless Void 2</a>'
        )
        self.assertEqual((report.chapter, report.title), (30, "Starless Void"))

    def test_legitimate_numeric_title_is_preserved_when_present_in_slug(self) -> None:
        report = self.check(
            '<a href="/chapter/shadow-slave/chapter-3120-catch-22">C3120 Catch 22</a>'
        )
        self.assertEqual((report.chapter, report.title), (3120, "Catch 22"))

    def test_relative_chapter_url_becomes_absolute_https(self) -> None:
        report = self.check(
            '<a href="/chapter/shadow-slave/chapter-3128-song-of-fire">C3128 Song of Fire</a>'
        )
        self.assertEqual(
            report.url,
            "https://novelarrow.com/chapter/shadow-slave/chapter-3128-song-of-fire",
        )


class TelegramParserTests(unittest.TestCase):
    def assert_telegra_title(self, url: str, chapter: int, title: str) -> None:
        report = parse_telegram_telegra_link(url)
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual((report.chapter, report.title, report.url), (chapter, title, url))

    def test_telegra_date_and_collision_suffixes_are_removed(self) -> None:
        cases = (
            ("https://telegra.ph/3126-Inferno-07-28-4", 3126, "Inferno"),
            ("https://telegra.ph/3115-Dark-and-Sinister-07-23-2", 3115, "Dark and Sinister"),
            ("https://telegra.ph/3116-Princess-of-the-Underworld-07-23-2", 3116, "Princess of the Underworld"),
        )
        for url, chapter, title in cases:
            with self.subTest(url=url):
                self.assert_telegra_title(url, chapter, title)

    def test_telegra_date_suffix_without_collision_is_removed(self) -> None:
        self.assert_telegra_title(
            "https://telegra.ph/3036-A-Feast-in-Time-of-Plague-06-11",
            3036,
            "A Feast in Time of Plague",
        )

    def test_legitimate_trailing_number_is_preserved(self) -> None:
        self.assert_telegra_title("https://telegra.ph/3120-Catch-22", 3120, "Catch 22")

    def test_matching_document_title_is_combined_with_telegra_url(self) -> None:
        url = "https://telegra.ph/3126-Inferno-07-28-4"
        html = f"""
            <div class="tgme_widget_message">
              <a class="tgme_widget_message_document_wrap" href="https://t.me/file/3126">
                <div class="tgme_widget_message_document_title">3126 Inferno.docx</div>
              </a>
              <a href="{url}">Read chapter</a>
            </div>
        """
        source = SourceConfig("Telegram", "https://t.me/s/shadow_slave_fastes", True, ("t.me",))

        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html):
            report = check_public_site(source)

        self.assertEqual((report.source, report.chapter, report.title, report.url), ("Telegram", 3126, "Inferno", url))

    def test_telegra_title_is_fallback_without_usable_document_title(self) -> None:
        url = "https://telegra.ph/3126-Inferno-07-28-4"
        html = f"""
            <div class="tgme_widget_message">
              <div class="tgme_widget_message_document_title">chapter-notes.docx</div>
              <a href="{url}">Read chapter</a>
            </div>
        """
        candidates = parse_telegram_candidates(BeautifulSoup(html, "html.parser"), "https://t.me/s/shadow_slave_fastes")

        self.assertEqual(len(candidates), 1)
        self.assertEqual((candidates[0].chapter, candidates[0].title, candidates[0].url), (3126, "Inferno", url))

    def test_document_title_takes_precedence_over_telegra_slug_title(self) -> None:
        url = "https://telegra.ph/3126-Inferno-Draft-07-28-4"
        html = f"""
            <div class="tgme_widget_message">
              <div class="tgme_widget_message_document_title">3126 Inferno.docx</div>
              <a href="{url}">Read chapter</a>
            </div>
        """
        candidates = parse_telegram_candidates(BeautifulSoup(html, "html.parser"), "https://t.me/s/shadow_slave_fastes")

        self.assertEqual(len(candidates), 1)
        self.assertEqual((candidates[0].title, candidates[0].url), ("Inferno", url))

    def test_earlier_ordinary_telegra_link_is_unchanged(self) -> None:
        self.assert_telegra_title(
            "https://telegra.ph/2986-A-Memory-Most-Dreadful-05-20",
            2986,
            "A Memory Most Dreadful",
        )


if __name__ == "__main__":
    unittest.main()
