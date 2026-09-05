from __future__ import annotations

import unittest
from unittest.mock import call, patch

from bs4 import BeautifulSoup

from shadow_slave_monitor.config import PUBLIC_SITES, SourceConfig
from shadow_slave_monitor.models import ChapterReport
from shadow_slave_monitor.parsers import (
    ParseError,
    check_public_site,
    parse_chikari_candidates,
    parse_chikari_chapter_title,
    parse_freewebnovel_candidates,
    check_lightnovelup,
    lightnovelup_candidate_from_href,
    parse_novel_phoenix_candidates,
    parse_novelfull_candidates,
    parse_readwn_candidates,
    parse_shadowslave_space_chapter_title,
    parse_telegram_candidates,
    parse_telegram_telegra_link,
)
from shadow_slave_monitor.state_manager import validate_source_config


class ChikariParserTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "Chikari")

    def test_exact_enabled_configuration_and_priority(self) -> None:
        validate_source_config()
        self.assertEqual(
            self.source,
            SourceConfig(
                "Chikari",
                "https://chikari.moe/novels/shadow-slave",
                True,
                ("chikari.moe", "www.chikari.moe"),
            ),
        )
        self.assertIs(PUBLIC_SITES[0], self.source)
        self.assertEqual(PUBLIC_SITES[1].name, "Telegram")
        self.assertEqual(
            {site.name: site.enabled for site in PUBLIC_SITES},
            {
                "Chikari": True,
                "Telegram": True,
                "Novel Buddy": True,
                "ShadowSlave.Space": True,
                "FreeWebNovel": True,
                "Readwn": True,
                "LightNovelUp": True,
                "Novel Phoenix": True,
                "NovelArrow": True,
                "NovelFire": True,
                "SSNovel": True,
                "NovelFull": True,
            },
        )


    def test_realistic_unordered_series_page_uses_highest_canonical_link(self) -> None:
        html = """
          <main><h1>Shadow Slave</h1><p>3149 chapters · 4.9 ratings · 123456 views</p>
          <p>Released 2026-08-09 · rank 7</p><nav>Page 1 2 3 2026 Next</nav>
          <section aria-label="Chapters">
            <a href="/novels/shadow-slave/3147">Chapter 3147</a>
            <a href="/novels/shadow-slave/3149">Latest</a>
            <a href="/novels/shadow-slave/3148">Chapter 3148</a>
            <a href="/novels/shadow-slave/3149">Duplicate</a>
            <a href="/novels/another-novel/9999">Other novel</a>
          </section><p>Chapter 9999 Plain text noise</p></main>
        """
        candidates = parse_chikari_candidates(BeautifulSoup(html, "html.parser"), self.source.url)
        self.assertEqual(len(candidates), 3)
        detail = "<h1>Chapter 3149 Loose Ends</h1>"
        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[html, detail]) as fetch:
            report = check_public_site(self.source)
        self.assertEqual(
            (report.source, report.chapter, report.title, report.url),
            ("Chikari", 3149, "Loose Ends", "https://chikari.moe/novels/shadow-slave/3149"),
        )
        self.assertEqual(
            fetch.call_args_list,
            [call(self.source), call(self.source, report.url)],
        )

    def test_allowed_absolute_hosts_relative_resolution_and_trailing_slash(self) -> None:
        html = "".join(
            (
                '<a href="/novels/shadow-slave/3147">relative</a>',
                '<a href="https://chikari.moe/novels/shadow-slave/3148">apex</a>',
                '<a href="https://www.chikari.moe/novels/shadow-slave/3149/">www</a>',
            )
        )
        candidates = parse_chikari_candidates(BeautifulSoup(html, "html.parser"), self.source.url)
        self.assertEqual([candidate.chapter for candidate in candidates], [3147, 3148, 3149])
        self.assertEqual(candidates[0].url, "https://chikari.moe/novels/shadow-slave/3147")

    def test_noncanonical_unsafe_and_malformed_links_are_rejected(self) -> None:
        invalid_hrefs = (
            "http://chikari.moe/novels/shadow-slave/3149",
            "https://evil.example/novels/shadow-slave/3149",
            "/novels/shadow-slave/3149?ref=latest",
            "/novels/shadow-slave/3149#comments",
            "/novels/shadow-slave/3149;session=1",
            "https://chikari.moe:443/novels/shadow-slave/3149",
            "/novels/other/3149",
            "/novels/shadow-slave/not-a-number",
            "/novels/shadow-slave/3149/extra",
            "/novels/shadow-slave",
            "/novels/shadow-slave/123456",
            "https://[broken/novels/shadow-slave/3149",
        )
        html = "".join(f'<a href="{href}">Chapter 9999</a>' for href in invalid_hrefs)
        self.assertEqual(parse_chikari_candidates(BeautifulSoup(html, "html.parser"), self.source.url), [])
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html):
            with self.assertRaises(ParseError):
                check_public_site(self.source)

    def test_plain_text_chapter_is_never_a_fallback(self) -> None:
        html = "<h2>Chapters</h2><p>Chapter 3149 Loose Ends</p><p>3149 chapters</p>"
        self.assertEqual(parse_chikari_candidates(BeautifulSoup(html, "html.parser"), self.source.url), [])
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html):
            with self.assertRaises(ParseError):
                check_public_site(self.source)

    def test_title_preserves_punctuation_unicode_and_legitimate_numbers(self) -> None:
        self.assertEqual(
            parse_chikari_chapter_title(
                "<h2> Chapter 3149 — Effie's Test-2: Who’s There?! Yes, No—Maybe 22 </h2>", 3149
            ),
            "Effie's Test-2: Who’s There?! Yes, No—Maybe 22",
        )

    def test_visible_h1_wins_over_earlier_suffixed_document_title(self) -> None:
        html = (
            "<html><head><title>Chapter 3149 Loose Ends · chikari.moe</title></head>"
            "<body><h1>Chapter 3149 Loose Ends</h1></body></html>"
        )
        self.assertEqual(parse_chikari_chapter_title(html, 3149), "Loose Ends")

    def test_h2_is_used_when_no_trustworthy_h1_exists(self) -> None:
        html = (
            "<title>Chapter 3149 Document Title | chikari.moe</title>"
            "<h1>Chapter 3150 Wrong Chapter</h1>"
            "<h2>Chapter 3149 Visible H2 Title</h2>"
        )
        self.assertEqual(parse_chikari_chapter_title(html, 3149), "Visible H2 Title")

    def test_document_title_is_final_fallback_and_removes_only_known_site_suffix(self) -> None:
        suffixes = (" · chikari.moe", "-chikari.moe", "  |  chikari.moe  ")
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                html = (
                    f"<title>Chapter 3149 Loose Ends{suffix}</title>"
                    "<h1>Shadow Slave</h1><h2>Chapter 3150 Wrong Chapter</h2>"
                )
                self.assertEqual(parse_chikari_chapter_title(html, 3149), "Loose Ends")

    def test_title_only_fallback_preserves_legitimate_punctuation(self) -> None:
        self.assertEqual(
            parse_chikari_chapter_title(
                "<title>Chapter 3149 Loose Ends · chikari.moe</title>", 3149
            ),
            "Loose Ends",
        )
        html = "<title>Chapter 3149 Love | War - Part 2?! · chikari.moe</title>"
        self.assertEqual(parse_chikari_chapter_title(html, 3149), "Love | War - Part 2?!")

    def test_title_requires_matching_number_and_trustworthy_structured_value(self) -> None:
        rejected = (
            "<h1>Chapter 3150 Loose Ends</h1>",
            "<h1>Chapter 3149</h1>",
            "<h1>Chapter 3149 12345</h1>",
            "<h1>Chapter 3149 Next</h1>",
            "<p>Chapter 3149 Loose Ends</p><h1>Shadow Slave</h1>",
        )
        for html in rejected:
            with self.subTest(html=html):
                self.assertIsNone(parse_chikari_chapter_title(html, 3149))

    def test_enrichment_failures_leave_trusted_chapter_and_url_unchanged(self) -> None:
        series = '<a href="/novels/shadow-slave/3149">Latest chapter</a>'
        details = ("<h1>Chapter 3150 Wrong</h1>", "<h1>broken", TimeoutError("secret"))
        for detail in details:
            with self.subTest(detail=detail):
                with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[series, detail]):
                    report = check_public_site(self.source)
                self.assertEqual(
                    (report.chapter, report.title, report.url),
                    (3149, None, "https://chikari.moe/novels/shadow-slave/3149"),
                )

        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[series, "<h1>ignored</h1>"]), \
                patch("shadow_slave_monitor.parsers.parse_chikari_chapter_title", side_effect=ValueError("bad html")):
            report = check_public_site(self.source)
        self.assertEqual((report.chapter, report.title, report.url),
                         (3149, None, "https://chikari.moe/novels/shadow-slave/3149"))


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

    def test_source_is_enabled_in_exact_configuration(self) -> None:
        validate_source_config()
        sites = list(PUBLIC_SITES)
        index = next(i for i, site in enumerate(sites) if site.name == "NovelArrow")
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


class NovelFullParserTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "NovelFull")

    def parse(self, html: str) -> list[ChapterReport]:
        return parse_novelfull_candidates(BeautifulSoup(html, "html.parser"), self.source.url)

    def test_relative_absolute_and_www_canonical_links_are_accepted(self) -> None:
        html = """
          <a href="/shadow-slave/chapter-3160-rushing-towards-a-nightmare.html">Chapter 3160 Rushing Towards a Nightmare</a>
          <a href="https://novelfull.com/shadow-slave/chapter-3161-a-new-dawn.html">Chapter 3161 — A New Dawn</a>
          <a href="https://www.novelfull.com/shadow-slave/chapter-3162-into-the-dark.html">Chapter 3162: Into the Dark</a>
        """
        candidates = self.parse(html)
        self.assertEqual([candidate.chapter for candidate in candidates], [3160, 3161, 3162])
        self.assertEqual(candidates[0].title, "Rushing Towards a Nightmare")
        self.assertEqual(
            candidates[0].url,
            "https://novelfull.com/shadow-slave/chapter-3160-rushing-towards-a-nightmare.html",
        )

    def test_titleless_short_canonical_link_has_no_invented_title(self) -> None:
        candidate = self.parse('<a href="/shadow-slave/chapter-15.html">Chapter 15</a>')[0]
        self.assertEqual((candidate.chapter, candidate.title), (15, None))

    def test_unordered_and_duplicate_links_select_highest_trusted_chapter(self) -> None:
        html = """
          <a href="/shadow-slave/chapter-3158-first-step.html">Chapter 3158: First Step</a>
          <a href="/shadow-slave/chapter-3160-rushing-towards-a-nightmare.html">Chapter 3160 Rushing Towards a Nightmare</a>
          <a href="/shadow-slave/chapter-3159-before-the-storm.html">Chapter 3159: Before the Storm</a>
          <a href="/shadow-slave/chapter-3160-rushing-towards-a-nightmare.html">Chapter 3160 Rushing Towards a Nightmare</a>
        """
        self.assertEqual([candidate.chapter for candidate in self.parse(html)], [3158, 3160, 3159])
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html):
            report = check_public_site(self.source)
        self.assertEqual((report.chapter, report.title), (3160, "Rushing Towards a Nightmare"))
        self.assertEqual(
            report.url,
            "https://novelfull.com/shadow-slave/chapter-3160-rushing-towards-a-nightmare.html",
        )

    def test_visible_chapter_must_match_url_and_title_is_cleaned_safely(self) -> None:
        html = """
          <a href="/shadow-slave/chapter-3147-forged.html">Chapter 9999: Forged</a>
          <a href="/shadow-slave/chapter-3147-unrelated-slug.html">Chapter 3147 — Effie's Test-2: Who’s There?! 22</a>
        """
        candidates = self.parse(html)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "Effie's Test-2: Who’s There?! 22")

    def test_canonical_link_without_chapter_label_does_not_invent_title(self) -> None:
        candidate = self.parse('<a href="/shadow-slave/chapter-3148-secret-title.html">Read latest</a>')[0]
        self.assertEqual((candidate.chapter, candidate.title), (3148, None))

    def test_noncanonical_unsafe_and_malformed_links_are_rejected(self) -> None:
        invalid_hrefs = (
            "https://evil.example/shadow-slave/chapter-3149.html",
            "http://novelfull.com/shadow-slave/chapter-3149.html",
            "https://novelfull.com/other-novel/chapter-3149.html",
            "/shadow-slave/chapter-3149.html?ref=latest",
            "/shadow-slave/chapter-3149.html#comments",
            "/shadow-slave/chapter-3149.html;session=1",
            "https://novelfull.com:443/shadow-slave/chapter-3149.html",
            "https://user@novelfull.com/shadow-slave/chapter-3149.html",
            "https://[broken/shadow-slave/chapter-3149.html",
            "/shadow-slave.html",
            "/shadow-slave/chapter-3149-.html",
            "/shadow-slave/chapter-3149-title.html/extra",
            "/shadow-slave/chapter-3149-title.htm",
            "/shadow-slave/chapter-3149--title.html",
        )
        html = "".join(f'<a href="{href}">Chapter 3149: Forged</a>' for href in invalid_hrefs)
        self.assertEqual(self.parse(html), [])

    def test_page_numbers_and_plain_text_never_become_candidates(self) -> None:
        html = """
          <h1>Shadow Slave</h1><p>3160 Chapters · 9999 views · rating 4.9</p>
          <p>Updated 2026-08-25 · rank 7</p><nav>Page 1 2 3 9999</nav>
          <p>Chapter 9999: Plain text noise</p>
          <a href="/shadow-slave.html">Chapter 9999: Index masquerade</a>
        """
        self.assertEqual(self.parse(html), [])
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html):
            with self.assertRaises(ParseError):
                check_public_site(self.source)

    def test_realistic_latest_chapters_block_reports_slugged_latest_link(self) -> None:
        html = """
          <div class="list-chapter">
            <h3>Latest chapters</h3>
            <ul>
              <li><a href="/shadow-slave/chapter-3159-before-the-storm.html">Chapter 3159 Before the Storm</a></li>
              <li><a href="/shadow-slave/chapter-3160-rushing-towards-a-nightmare.html">Chapter 3160 Rushing Towards a Nightmare</a></li>
            </ul>
          </div>
        """
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html):
            report = check_public_site(self.source)
        self.assertEqual((report.chapter, report.title), (3160, "Rushing Towards a Nightmare"))


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


class ReadwnParserTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "Readwn")

    def check(self, html: str) -> ChapterReport:
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html):
            return check_public_site(self.source)

    def test_enabled_configuration_and_semantic_latest_section(self) -> None:
        self.assertEqual(
            self.source,
            SourceConfig("Readwn", "https://readwn.org/book/shadow-slave", True,
                         ("readwn.org", "www.readwn.org")),
        )
        html = '''
          <a href="/book/shadow-slave/chapter-9999-noise">Chapter 9999 Noise</a>
          <section><h2>6 Latest Chapters</h2><div>
            <a href="/book/shadow-slave/chapter-3173-life-goes-on">Chapter 3173 Life Goes On</a>
            <a href="/book/shadow-slave/chapter-3174-tatals-basilisk">Chapter 3174 — Tatal's Basilisk</a>
          </div></section>
          <p>999999 views · 2026-09-05 · 4.9 rating · page 8888</p>
        '''
        report = self.check(html)
        self.assertEqual((report.chapter, report.title, report.url),
                         (3174, "Tatal's Basilisk",
                          "https://readwn.org/book/shadow-slave/chapter-3174-tatals-basilisk"))

    def test_relative_absolute_and_titleless_canonical_links(self) -> None:
        html = '''<section><h3>Latest Release</h3><div>
          <a href="https://www.readwn.org/book/shadow-slave/chapter-3173-life-goes-on">Latest chapter</a>
          <a href="/book/shadow-slave/chapter-3174-tatals-basilisk">Read now</a>
        </div></section>'''
        candidates = parse_readwn_candidates(BeautifulSoup(html, "html.parser"), self.source.url)
        self.assertEqual([item.chapter for item in candidates], [3173, 3174])
        self.assertIsNone(candidates[1].title)

    def test_untrusted_latest_information_fails_closed(self) -> None:
        bad_links = (
            '<a href="/book/shadow-slave/chapter-3173-life">Chapter 3174 Wrong</a>',
            '<a href="https://evil.example/book/shadow-slave/chapter-3174-title">Chapter 3174 Title</a>',
            '<a href="/book/shadow-slave/3174-title">Chapter 3174 Title</a>',
            '<a href="http://readwn.org/book/shadow-slave/chapter-3174-title">Chapter 3174 Title</a>',
            '<a href="/book/shadow-slave/chapter-3174-title?next=9999">Chapter 3174 Title</a>',
        )
        for link in bad_links:
            html = f"<section><h2>Latest Chapters</h2><div>{link}</div></section><p>Chapter 9999 Noise</p>"
            with self.subTest(link=link), self.assertRaises(ParseError):
                self.check(html)

    def test_missing_semantic_latest_area_does_not_scan_other_links(self) -> None:
        html = '<a href="/book/shadow-slave/chapter-3174-tatals-basilisk">Chapter 3174 Tatal\'s Basilisk</a>'
        with self.assertRaises(ParseError):
            self.check(html)


class LightNovelUpParserTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "LightNovelUp")
    chapter_3173 = "https://lightnovelup.com/novel/shadow-slave/chapter-3173-life-goes-on/"
    chapter_3174 = "https://lightnovelup.com/novel/shadow-slave/chapter-3174-tatals-basilisk/"

    @staticmethod
    def page(chapter: int, title: str, next_href: str | None = None, *, duplicate_next: bool = False) -> str:
        before = f'<a href="{next_href}">Next</a>' if next_href else '<a href="/previous">Prev</a>'
        after = f'<a href="{next_href}">Next</a>' if next_href and duplicate_next else ""
        return f"<html><head><title>Shadow Slave - Chapter {chapter} {title} - Light Novel Fastest Update</title></head><body>{before}<article><h1>Shadow Slave - Chapter {chapter} {title}</h1><p>Chapter body</p></article>{after}</body></html>"

    def test_enabled_configuration(self) -> None:
        self.assertEqual(self.source, SourceConfig(
            "LightNovelUp", "https://lightnovelup.com/novel/shadow-slave/", True,
            ("lightnovelup.com", "www.lightnovelup.com"),
        ))

    def test_read_last_on_novel_page_is_not_chapter_discovery(self) -> None:
        href = "https://lightnovelup.com/novel/shadow-slave/"
        self.assertIsNone(lightnovelup_candidate_from_href(href, self.source.url))

    def test_live_navigation_bootstraps_to_3174_and_extracts_visible_title(self) -> None:
        pages = [self.page(3173, "Life Goes On", self.chapter_3174, duplicate_next=True),
                 self.page(3174, "Tatal’s Basilisk")]
        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=pages) as fetch:
            report = check_lightnovelup(self.source)
        self.assertEqual((report.chapter, report.title, report.url),
                         (3174, "Tatal’s Basilisk", self.chapter_3174))
        self.assertEqual((report.position_chapter, report.position_url), (3174, self.chapter_3174))
        self.assertEqual(fetch.call_args_list,
                         [call(self.source, self.chapter_3173), call(self.source, self.chapter_3174)])

    def test_saved_latest_cursor_is_quiet_and_hypothetical_next_advances(self) -> None:
        cursor = {"chapter": 3174, "url": self.chapter_3174}
        with patch("shadow_slave_monitor.parsers.fetch_html",
                   return_value=self.page(3174, "Tatal’s Basilisk")) as fetch:
            report = check_lightnovelup(self.source, cursor)
        self.assertEqual(report.position_chapter, 3174)
        fetch.assert_called_once_with(self.source, self.chapter_3174)

        chapter_3175 = "https://lightnovelup.com/novel/shadow-slave/chapter-3175-a-real-site-slug/"
        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[
            self.page(3174, "Tatal’s Basilisk", chapter_3175),
            self.page(3175, "A Real Site Title"),
        ]):
            advanced = check_lightnovelup(self.source, cursor)
        self.assertEqual((advanced.chapter, advanced.url), (3175, chapter_3175))

    def test_invalid_next_navigation_fails_closed(self) -> None:
        bad_next = (
            self.chapter_3173,
            "https://lightnovelup.com/novel/shadow-slave/chapter-3172-little-flames/",
            "https://evil.example/novel/shadow-slave/chapter-3174-title/",
            "https://lightnovelup.com/novel/other/chapter-3174-title/",
            "https://lightnovelup.com/novel/shadow-slave/chapter-3199-jump/",
            "https://lightnovelup.com/novel/shadow-slave/chapter-3174-title/?query=bad",
        )
        for href in bad_next:
            with self.subTest(href=href), patch("shadow_slave_monitor.parsers.fetch_html",
                                                return_value=self.page(3173, "Life Goes On", href)), self.assertRaises(ParseError):
                check_lightnovelup(self.source)

    def test_heading_disagreement_and_ambiguous_next_fail_closed(self) -> None:
        with patch("shadow_slave_monitor.parsers.fetch_html",
                   return_value=self.page(3172, "Wrong", self.chapter_3174)), self.assertRaises(ParseError):
            check_lightnovelup(self.source)
        different_destination = "https://lightnovelup.com/novel/shadow-slave/chapter-3174-a-different-slug/"
        ambiguous = self.page(3173, "Life Goes On", self.chapter_3174).replace(
            "</body>", f'<a href="{different_destination}">Next Chapter</a></body>')
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=ambiguous), self.assertRaises(ParseError):
            check_lightnovelup(self.source)

    def test_cycle_and_traversal_limit_fail_closed(self) -> None:
        from shadow_slave_monitor import parsers
        with patch.object(parsers, "LIGHTNOVELUP_MAX_TRAVERSAL", 1), \
             patch("shadow_slave_monitor.parsers.fetch_html",
                   return_value=self.page(3173, "Life Goes On", self.chapter_3174)), \
             self.assertRaisesRegex(ParseError, "traversal limit"):
            check_lightnovelup(self.source)

    def test_canonical_url_examples_and_malformed_urls(self) -> None:
        examples = (self.chapter_3174, self.chapter_3173,
                    "https://lightnovelup.com/novel/shadow-slave/chapter-3171-wedding-of-the-century/",
                    "https://lightnovelup.com/novel/shadow-slave/chapter-3153-an-army-a-fortress-and-a-general/")
        self.assertEqual([lightnovelup_candidate_from_href(url, self.source.url).chapter for url in examples],
                         [3174, 3173, 3171, 3153])
        for url in ("http://lightnovelup.com/novel/shadow-slave/chapter-3174-title/",
                    "https://lightnovelup.com:443/novel/shadow-slave/chapter-3174-title/",
                    "https://lightnovelup.com/novel/shadow-slave/chapter-3174-title/#bad",
                    "https://lightnovelup.com/novel/shadow-slave/chapter-3174-%74itle/"):
            self.assertIsNone(lightnovelup_candidate_from_href(url, self.source.url))


if __name__ == "__main__":
    unittest.main()
