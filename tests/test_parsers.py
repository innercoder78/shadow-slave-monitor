from __future__ import annotations

import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from shadow_slave_monitor.config import PUBLIC_SITES, SourceConfig
from shadow_slave_monitor.models import ChapterReport
from shadow_slave_monitor.parsers import (
    ParseError,
    check_public_site,
    parse_freewebnovel_candidates,
    parse_novel_phoenix_candidates,
    parse_shadowslave_space_chapter_title,
    parse_telegram_candidates,
    parse_telegram_telegra_link,
)
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

    def test_disabled_configuration_and_main_latest_card(self) -> None:
        validate_source_config()
        self.assertFalse(self.source.enabled)
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

    def test_source_is_disabled_in_exact_configuration(self) -> None:
        validate_source_config()
        sites = list(PUBLIC_SITES)
        index = next(i for i, site in enumerate(sites) if site.name == "NovelArrow")
        self.assertFalse(sites[index].enabled)
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


class ShadowSlaveSpaceParserTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "ShadowSlave.Space")

    def check(self, html: str):
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html):
            return check_public_site(self.source)

    def test_exact_enabled_configuration_after_novel_buddy(self) -> None:
        validate_source_config()
        sites = list(PUBLIC_SITES)
        index = sites.index(self.source)
        self.assertEqual(sites[index - 1].name, "Novel Buddy")
        self.assertEqual(
            self.source,
            SourceConfig(
                "ShadowSlave.Space",
                "https://shadowslave.space",
                True,
                ("shadowslave.space", "www.shadowslave.space"),
            ),
        )

    def test_realistic_unordered_homepage_uses_highest_canonical_link(self) -> None:
        html = """
            <h2>Latest Chapters</h2>
            <p>Showing 1-20 of 3144 chapters</p>
            <a href="/chapters/3143">3143 Shadow Slave Chapter 3143 <span>New</span></a>
            <a href="/chapters/3144">3144 Shadow Slave Chapter 3144 <span>New</span></a>
            <a href="/chapters/3142">3142 Shadow Slave Chapter 3142</a>
        """
        report = self.check(html)
        self.assertEqual(
            (report.source, report.chapter, report.title, report.url),
            ("ShadowSlave.Space", 3144, None, "https://shadowslave.space/chapters/3144"),
        )

    def test_highest_chapter_is_enriched_with_exactly_one_selected_detail_request(self) -> None:
        homepage = """
            <a href="/chapters/3143/">3143 Shadow Slave Chapter 3143 New</a>
            <a href="/chapters/3144/">3144 Shadow Slave Chapter 3144 New</a>
            <a href="/chapters/3142/">3142 Shadow Slave Chapter 3142</a>
        """
        detail = "<html><head><title>Shadow Slave</title></head><body><h1>Shadow Slave Chapter 3144 - Chapter 3144 The Gathering of Demigods</h1></body></html>"
        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[homepage, detail]) as fetch:
            report = check_public_site(self.source)

        self.assertEqual(
            (report.chapter, report.title, report.url),
            (3144, "The Gathering of Demigods", "https://shadowslave.space/chapters/3144/"),
        )
        self.assertEqual(fetch.call_count, 2)
        fetch.assert_any_call(self.source)
        fetch.assert_any_call(self.source, "https://shadowslave.space/chapters/3144/")

    def test_known_duplicated_heading_formats_are_normalized(self) -> None:
        cases = (
            (3116, "Shadow Slave Chapter 3116 - Chapter 3116 Princess of the Underworld", "Princess of the Underworld"),
            (2026, "Shadow Slave Chapter 2026 - Chapter 2026 - 2026: Escalation", "Escalation"),
        )
        for chapter, heading, expected in cases:
            with self.subTest(chapter=chapter):
                self.assertEqual(
                    parse_shadowslave_space_chapter_title(f"<h2>{heading}</h2>", chapter),
                    expected,
                )

    def test_detail_title_requires_matching_chapter_and_rejects_labels_and_metadata(self) -> None:
        rejected = (
            "<h1>Shadow Slave Chapter 3143 - Chapter 3143 Wrong Chapter</h1>",
            "<h1>Shadow Slave Chapter 3144 - Chapter 3144</h1>",
            "<h1>Shadow Slave Chapter 3144 - Chapter 3144 - 3144</h1>",
            "<p>Shadow Slave Chapter 3144 - Chapter 3144 Metadata</p><h1>1234 views</h1>",
            "<nav>Shadow Slave Chapter 3144 - Chapter 3144 Navigation</nav><h2>88 comments</h2>",
            "<title>Shadow Slave Chapter 3144 - Read Online</title><div>4.9 ratings 2026-08-08</div>",
        )
        for html in rejected:
            with self.subTest(html=html):
                self.assertIsNone(parse_shadowslave_space_chapter_title(html, 3144))

    def test_detail_fetch_failure_and_malformed_parser_fail_soft(self) -> None:
        homepage = '<a href="/chapters/3144/">3144 Shadow Slave Chapter 3144 New</a>'
        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[homepage, TimeoutError("secret")]):
            report = check_public_site(self.source)
        self.assertEqual((report.chapter, report.title, report.url),
                         (3144, None, "https://shadowslave.space/chapters/3144/"))

        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[homepage, "<h1>broken"]), \
                patch("shadow_slave_monitor.parsers.parse_shadowslave_space_chapter_title", side_effect=ValueError("bad html")):
            report = check_public_site(self.source)
        self.assertEqual((report.chapter, report.title), (3144, None))

    def test_trailing_slash_relative_url_and_duplicates_are_supported(self) -> None:
        report = self.check(
            '<a href="/chapters/3144/">New</a>'
            '<a href="/chapters/3144/">3144 Shadow Slave Chapter 3144 New</a>'
        )
        self.assertEqual((report.chapter, report.title, report.url),
                         (3144, None, "https://shadowslave.space/chapters/3144/"))

    def test_page_metadata_is_not_a_candidate(self) -> None:
        html = """
            <p>Showing 1-20 of 9999 chapters</p><p>2026-08-08</p>
            <p>4.9 rating · 1234 views · 88 comments</p>
            <nav>1 2 3 100 Next</nav><p>Updated 12 minutes ago</p>
            <a href="/chapters/3144">3144 Shadow Slave Chapter 3144 New</a>
        """
        report = self.check(html)
        self.assertEqual((report.chapter, report.title), (3144, None))

    def test_noncanonical_links_are_rejected(self) -> None:
        invalid_hrefs = (
            "/chapters/3144/extra",
            "/chapters//3144",
            "/chapter/3144",
            "/chapters/3144?ref=latest",
            "/chapters/3144#comments",
            "https://example.com/chapters/3144",
            "http://shadowslave.space/chapters/3144",
            "https://shadowslave.space:443/chapters/3144",
        )
        for href in invalid_hrefs:
            with self.subTest(href=href), self.assertRaises(ParseError):
                self.check(f'<a href="{href}">Chapter 3144 New</a>')

    def test_no_trustworthy_chapter_link_raises_parse_error(self) -> None:
        with self.assertRaises(ParseError):
            self.check(
                "<h2>Latest Chapters</h2><p>Showing 1-20 of 3144 chapters</p>"
                "<p>Chapter 3144 New · 3144 views</p>"
            )


class FreeWebNovelParserTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "FreeWebNovel")

    def check(self, html: str):
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html) as fetch:
            report = check_public_site(self.source)
        fetch.assert_called_once_with(self.source)
        return report

    def test_exact_enabled_configuration_immediately_after_shadowslave_space(self) -> None:
        validate_source_config()
        sites = list(PUBLIC_SITES)
        index = sites.index(self.source)
        self.assertEqual(sites[index - 1].name, "ShadowSlave.Space")
        self.assertEqual(
            self.source,
            SourceConfig(
                "FreeWebNovel",
                "https://freewebnovel.com/novel/shadow-slave",
                True,
                ("freewebnovel.com", "www.freewebnovel.com"),
            ),
        )

    def test_realistic_latest_section_selects_highest_and_extracts_exact_title(self) -> None:
        html = """
          <section class="latest-chapters">
            <h2>6 Latest Chapters</h2>
            <div>
              <a href="/novel/shadow-slave/chapter-3143">Chapter 3143 Coronation</a>
              <a href="/novel/shadow-slave/chapter-3144">Chapter 3144 The Gathering of Demigods</a>
              <a href="/novel/shadow-slave/chapter-3142">Chapter 3142 Far Away</a>
            </div>
          </section>
        """
        report = self.check(html)
        self.assertEqual(
            (report.source, report.chapter, report.title, report.url),
            ("FreeWebNovel", 3144, "The Gathering of Demigods",
             "https://freewebnovel.com/novel/shadow-slave/chapter-3144"),
        )

    def test_absolute_www_url_trailing_slash_and_title_punctuation_numbers(self) -> None:
        report = self.check(
            '<h2>12 Latest Chapters</h2>'
            '<div><a href="https://www.freewebnovel.com/novel/shadow-slave/chapter-3144/">'
            "Chapter 3144 Effie&apos;s Test-2: Shut the Gates, Now?!</a></div>"
        )
        self.assertEqual(report.title, "Effie's Test-2: Shut the Gates, Now?!")
        self.assertEqual(report.url, "https://www.freewebnovel.com/novel/shadow-slave/chapter-3144/")

    def test_duplicate_links_are_deduplicated(self) -> None:
        html = (
            '<h2>Latest Chapters</h2><div>'
            '<a href="/novel/shadow-slave/chapter-3144">Chapter 3144 The Gathering of Demigods</a>'
            '<a href="/novel/shadow-slave/chapter-3144">Chapter 3144 The Gathering of Demigods</a>'
            "</div>"
        )
        candidates = parse_freewebnovel_candidates(
            BeautifulSoup(html, "html.parser"), self.source.url
        )
        self.assertEqual(len(candidates), 1)

    def test_latest_section_is_preferred_over_higher_catalog_link(self) -> None:
        html = """
          <a href="/novel/shadow-slave/chapter-9999">Chapter 9999 Catalog Entry</a>
          <h3>8 Latest Chapters</h3>
          <div><a href="/novel/shadow-slave/chapter-3144">Chapter 3144 Current Title</a></div>
          <aside><a href="/novel/other/chapter-9998">Chapter 9998 Other Novel</a></aside>
        """
        self.assertEqual(self.check(html).chapter, 3144)

    def test_visible_and_href_chapter_numbers_must_match(self) -> None:
        html = """
          <h2>Latest Chapters</h2><div>
            <a href="/novel/shadow-slave/chapter-3144">Chapter 3145 Wrong</a>
            <a href="/novel/shadow-slave/chapter-3143">Chapter 3143 Coronation</a>
          </div>
        """
        self.assertEqual(self.check(html).chapter, 3143)

    def test_noncanonical_and_unsafe_links_are_rejected(self) -> None:
        invalid_hrefs = (
            "http://freewebnovel.com/novel/shadow-slave/chapter-3144",
            "https://evil.example/novel/shadow-slave/chapter-3144",
            "/novel/shadow-slave/chapter-3144?ref=latest",
            "/novel/shadow-slave/chapter-3144#comments",
            "/novel/shadow-slave/chapter-3144;session=1",
            "/novel/other/chapter-3144",
            "/novel/shadow-slave/chapters-3144",
            "/novel/shadow-slave/chapter-3144/extra",
            "https://freewebnovel.com:443/novel/shadow-slave/chapter-3144",
        )
        for href in invalid_hrefs:
            with self.subTest(href=href), self.assertRaises(ParseError):
                self.check(f'<a href="{href}">Chapter 3144 Valid Title</a>')

    def test_unrelated_numbers_and_ui_only_titles_are_not_candidates(self) -> None:
        html = """
          <h2>Latest Chapters</h2><div>
            <p>4.9 ratings · 88 votes · 1234 views · 2026-08-09 · page 99</p>
            <a href="/novel/other/chapter-9999">Chapter 9999 Other Novel</a>
            <a href="/novel/shadow-slave/chapter-3144">Chapter 3144 12345</a>
          </div>
        """
        with self.assertRaises(ParseError):
            self.check(html)

    def test_no_trustworthy_links_uses_normal_parse_error(self) -> None:
        with self.assertRaises(ParseError):
            self.check("<h2>Latest Chapters</h2><p>Chapter 3144 The Gathering of Demigods</p>")


class NovelPhoenixParserTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "Novel Phoenix")

    def check(self, html: str):
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html) as fetch:
            report = check_public_site(self.source)
        fetch.assert_called_once_with(self.source)
        return report

    def test_exact_enabled_configuration_immediately_after_freewebnovel(self) -> None:
        validate_source_config()
        sites = list(PUBLIC_SITES)
        index = sites.index(self.source)
        self.assertEqual(sites[index - 1].name, "FreeWebNovel")
        self.assertEqual(
            self.source,
            SourceConfig(
                "Novel Phoenix",
                "https://novelphoenix.com/novel/shadow-slave/chapters",
                True,
                ("novelphoenix.com", "www.novelphoenix.com"),
            ),
        )

    def test_realistic_latest_release_returns_exact_report_and_ignores_noise(self) -> None:
        catalog = "".join(
            f'<a href="/novel/shadow-slave/chapter-{chapter}">Chapter {chapter} Catalog Title</a>'
            for chapter in range(1, 101)
        )
        html = f"""
          <p>A total of 3148 chapters have been translated</p>
          <p>Release date 2026-08-09 · rank 7 · 4.9 rating · 123456 views</p>
          <nav>1 2 3 31 32 Next</nav>
          <section class="release-card">
            <span>Latest Release:</span>
            <a href="/novel/shadow-slave/chapter-3148">Chapter 3148 Division of Power</a>
            <small>Updated 2 hours ago</small>
          </section>
          <section class="catalog">{catalog}
            <a href="/novel/shadow-slave/chapter-9999">Chapter 9999 Bogus Catalog Entry</a>
          </section>
        """
        report = self.check(html)
        self.assertEqual(
            (report.source, report.chapter, report.title, report.url),
            (
                "Novel Phoenix",
                3148,
                "Division of Power",
                "https://novelphoenix.com/novel/shadow-slave/chapter-3148",
            ),
        )

    def test_absolute_https_url_and_optional_trailing_slash_are_accepted(self) -> None:
        for url in (
            "https://novelphoenix.com/novel/shadow-slave/chapter-3148",
            "https://www.novelphoenix.com/novel/shadow-slave/chapter-3148/",
        ):
            with self.subTest(url=url):
                report = self.check(
                    f'<div><b>Latest Release</b><a href="{url}">Chapter 3148 Division of Power</a></div>'
                )
                self.assertEqual(report.url, url)

    def test_title_preserves_legitimate_punctuation_unicode_and_numbers(self) -> None:
        report = self.check(
            '<div><span>Latest Release:</span>'
            '<a href="/novel/shadow-slave/chapter-3148">'
            "Chapter 3148 Effie's Test-2: Who’s There?! Yes, No—Maybe</a></div>"
        )
        self.assertEqual(report.title, "Effie's Test-2: Who’s There?! Yes, No—Maybe")

    def test_nearby_update_text_is_not_part_of_title(self) -> None:
        report = self.check(
            '<div><span>Latest Release:</span>'
            '<a href="/novel/shadow-slave/chapter-3148">Chapter 3148 Division of Power</a>'
            '<span>Updated 2 hours ago</span></div>'
        )
        self.assertEqual(report.title, "Division of Power")

    def test_latest_release_marker_is_case_and_whitespace_tolerant(self) -> None:
        for marker in ("Latest Release:", "Latest Release", "  LATEST   RELEASE:  "):
            with self.subTest(marker=marker):
                report = self.check(
                    f'<section><h3>{marker}</h3><div>'
                    '<a href="/novel/shadow-slave/chapter-3148">Chapter 3148 Division of Power</a>'
                    "</div></section>"
                )
                self.assertEqual(report.chapter, 3148)

    def test_visible_and_url_chapter_numbers_must_match(self) -> None:
        with self.assertRaises(ParseError):
            self.check(
                '<div><span>Latest Release:</span>'
                '<a href="/novel/shadow-slave/chapter-3148">Chapter 3149 Division of Power</a></div>'
            )

    def test_noncanonical_and_unsafe_links_are_rejected(self) -> None:
        invalid_hrefs = (
            "http://novelphoenix.com/novel/shadow-slave/chapter-3148",
            "https://evil.example/novel/shadow-slave/chapter-3148",
            "/novel/shadow-slave/chapter-3148?ref=latest",
            "/novel/shadow-slave/chapter-3148#comments",
            "/novel/shadow-slave/chapter-3148;session=1",
            "/novel/other/chapter-3148",
            "/novel/shadow-slave/chapters",
            "/novel/shadow-slave/chapter-",
            "/novel/shadow-slave/chapter-3148-title",
            "/novel/shadow-slave/chapter-3148/extra",
            "https://novelphoenix.com:443/novel/shadow-slave/chapter-3148",
        )
        for href in invalid_hrefs:
            with self.subTest(href=href), self.assertRaises(ParseError):
                self.check(
                    f'<div><span>Latest Release:</span><a href="{href}">'
                    "Chapter 3148 Division of Power</a></div>"
                )

    def test_missing_title_is_rejected(self) -> None:
        with self.assertRaises(ParseError):
            self.check(
                '<div><span>Latest Release:</span>'
                '<a href="/novel/shadow-slave/chapter-3148">Chapter 3148</a></div>'
            )

    def test_catalog_links_without_marker_do_not_become_candidates(self) -> None:
        html = """
          <p>A total of 3148 chapters have been translated</p>
          <p>Release date 2026-08-09 · 4.9 rating · 123456 views · rank 7</p>
          <nav>1 2 3 31 32 Next</nav>
          <a href="/novel/shadow-slave/chapter-100">Chapter 100 Catalog Title</a>
          <a href="/novel/shadow-slave/chapter-3148">Chapter 3148 Division of Power</a>
          <a href="/novel/shadow-slave/chapter-9999">Chapter 9999 Bogus Catalog Entry</a>
        """
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(parse_novel_phoenix_candidates(soup, self.source.url), [])
        with self.assertRaises(ParseError):
            self.check(html)


class PublicSourceLoggingTests(unittest.TestCase):
    def test_novelfull_success_log_always_includes_detected_url(self) -> None:
        source = next(site for site in PUBLIC_SITES if site.name == "NovelFull")
        candidate = ChapterReport("", 3144, "The Gathering of Demigods", "https://novelfull.com/shadow-slave/chapter-3144.html")
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value="<html></html>"), \
                patch("shadow_slave_monitor.parsers.iter_public_candidates", return_value=[candidate]), \
                self.assertLogs(level="INFO") as logs:
            check_public_site(source)
        self.assertIn(
            "NovelFull reports chapter 3144: The Gathering of Demigods (https://novelfull.com/shadow-slave/chapter-3144.html)",
            "\n".join(logs.output),
        )

    def test_success_log_includes_url_when_title_is_missing(self) -> None:
        source = SourceConfig("Other Public Source", "https://example.com", True, ("example.com",))
        candidate = ChapterReport("", 3144, None, "https://example.com/chapter/3144")
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value="<html></html>"), \
                patch("shadow_slave_monitor.parsers.iter_public_candidates", return_value=[candidate]), \
                self.assertLogs(level="INFO") as logs:
            check_public_site(source)
        self.assertIn(
            "Other Public Source reports chapter 3144: (no title) (https://example.com/chapter/3144)",
            "\n".join(logs.output),
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
