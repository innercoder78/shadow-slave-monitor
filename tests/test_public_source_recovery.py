from __future__ import annotations

from datetime import UTC, datetime
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from shadow_slave_monitor import monitor
from shadow_slave_monitor.config import PUBLIC_SITES, SourceConfig
from shadow_slave_monitor.models import ChapterReport, RunResult
from shadow_slave_monitor.parsers import (
    parse_freewebnovel_candidates,
    parse_freewebnovel_net_candidates,
    parse_novel_buddy_candidates,
    parse_novelfull_candidates,
    parse_novelarrow_candidates,
    parse_readnovelfull_candidates,
    parse_rechapters_candidates,
    parse_telegram_candidates,
    parse_telegram_telegra_link,
)


CONTEXT = (3190, "Freedom of Choice", 3189, "Entertaining Guest")


class PreviousContextParserTests(unittest.TestCase):
    def assert_transition(self, parser, base_url: str, waiting: str, released: str) -> None:
        waiting_reports = parser(BeautifulSoup(waiting, "html.parser"), base_url, *CONTEXT)
        released_reports = parser(BeautifulSoup(released, "html.parser"), base_url, *CONTEXT)
        self.assertEqual(max(report.chapter for report in waiting_reports), 3189)
        self.assertEqual(max(report.chapter for report in released_reports), 3190)

    def test_sequential_html_sources_use_only_trusted_previous_context(self) -> None:
        cases = (
            (parse_novelfull_candidates, "https://novelfull.com/shadow-slave.html",
             "/shadow-slave/chapter-{slug}.html", "<h2>Latest chapters</h2>"),
            (parse_freewebnovel_net_candidates, "https://freewebnovel.net/shadow-slave.html",
             "/shadow-slave/chapter-{slug}.html", "<h2>6 Latest Chapters [ Updated an hour ago ]</h2>"),
        )
        for parser, base, path, heading in cases:
            previous = f'<a href="{path.format(slug="entertaining-guest")}">Chapter Entertaining Guest</a>'
            numbered = f'<a href="{path.format(slug="3188-lost-soul")}">Chapter 3188 Lost Soul</a>'
            target = f'<a href="{path.format(slug="freedom-of-choice")}">Chapter Freedom of Choice</a>'
            with self.subTest(parser=parser.__name__):
                self.assert_transition(parser, base, f"<section>{heading}{previous}{numbered}</section>",
                                       f"<section>{heading}{target}{previous}{numbered}</section>")

    def test_nested_production_shapes_preserve_newest_order(self) -> None:
        cases = (
            (parse_freewebnovel_candidates, "https://freewebnovel.com/novel/shadow-slave",
             "/novel/shadow-slave/chapter-{slug}", "6 Latest Chapters [ Updated now ]"),
            (parse_novelfull_candidates, "https://novelfull.com/shadow-slave.html",
             "/shadow-slave/chapter-{slug}.html", "Latest chapters"),
            (parse_freewebnovel_net_candidates, "https://freewebnovel.net/shadow-slave.html",
             "/shadow-slave/chapter-{slug}.html", "6 Latest Chapters [ Updated now ]"),
            (parse_novel_buddy_candidates, "https://novelbuddy.me/shadow-slave",
             "/shadow-slave/chapter-{slug}", "Newest"),
        )
        for parser, base, path, heading in cases:
            def page(first_title: str, first_slug: str, include_previous: bool) -> str:
                previous = (f'<li><a href="{path.format(slug="entertaining-guest")}">'
                            'Chapter Entertaining Guest</a></li>') if include_previous else ""
                return (f'<section><div class="heading"><h3><span>{heading}</span></h3></div><ul>'
                        f'<li><a href="{path.format(slug=first_slug)}">Chapter {first_title}</a></li>'
                        f'{previous}<li><a href="{path.format(slug="3188-lost-soul")}">'
                        'Chapter 3188 Lost Soul</a></li></ul><h3>Chapter List</h3>'
                        '<a href="/shadow-slave/chapter-9999-bad">Chapter 9999 Bad</a></section>')
            waiting = page("Entertaining Guest", "3189" if parser is parse_freewebnovel_candidates else "entertaining-guest", False)
            released = page("Freedom of Choice", "3190" if parser is parse_freewebnovel_candidates else "freedom-of-choice", True)
            with self.subTest(parser=parser.__name__):
                self.assert_transition(parser, base, waiting, released)

    def test_nested_sections_fail_closed_on_unknown_or_duplicate_newest(self) -> None:
        base = "https://novelbuddy.me/shadow-slave"
        unknown = ('<section><h3><span>Newest</span></h3><ul>'
                   '<li><a href="/shadow-slave/chapter-unknown-arrival">Chapter Unknown Arrival</a></li>'
                   '<li><a href="/shadow-slave/chapter-freedom-of-choice">Chapter Freedom of Choice</a></li>'
                   '</ul></section>')
        self.assertEqual(parse_novel_buddy_candidates(
            BeautifulSoup(unknown, "html.parser"), base, *CONTEXT), [])
        duplicated = unknown + '<section><h3>Newest</h3></section>'
        self.assertEqual(parse_novel_buddy_candidates(
            BeautifulSoup(duplicated, "html.parser"), base, *CONTEXT), [])

    def test_duplicate_markers_never_fall_back_to_global_candidates(self) -> None:
        cases = (
            (parse_freewebnovel_candidates, "https://freewebnovel.com/novel/shadow-slave",
             "6 Latest Chapters", "/novel/shadow-slave/chapter-3188"),
            (parse_novelfull_candidates, "https://novelfull.com/shadow-slave.html",
             "Latest chapters", "/shadow-slave/chapter-3188-lost-soul.html"),
            (parse_freewebnovel_net_candidates, "https://freewebnovel.net/shadow-slave.html",
             "6 Latest Chapters", "/shadow-slave/chapter-3188-lost-soul.html"),
            (parse_novel_buddy_candidates, "https://novelbuddy.me/shadow-slave",
             "Newest", "/shadow-slave/chapter-3188-lost-soul"),
        )
        for parser, base, marker, numbered_url in cases:
            html = (f'<section><h3>{marker}</h3><a href="{numbered_url}">Chapter 3188 Lost Soul</a></section>'
                    f'<section><h3>{marker}</h3><a href="{numbered_url}">Chapter 3188 Lost Soul</a></section>'
                    '<aside><a href="/shadow-slave/chapter-freedom-of-choice.html">'
                    'Chapter Freedom of Choice</a></aside>')
            with self.subTest(parser=parser.__name__):
                self.assertEqual(parser(BeautifulSoup(html, "html.parser"), base, *CONTEXT), [])

    def test_read_latest_reports_known_previous(self) -> None:
        source = next(site for site in PUBLIC_SITES if site.name == "ReadNovelFull")
        html = '<section><h3>Latest chapter</h3><a href="/shadow-slave/chapter-entertaining-guest.html">Chapter Entertaining Guest</a></section>'
        reports = parse_readnovelfull_candidates(BeautifulSoup(html, "html.parser"), source.url, *CONTEXT)
        self.assertEqual([(r.chapter, r.title) for r in reports], [(3189, "Entertaining Guest")])

    def test_single_latest_opaque_link_reports_previous_then_target(self) -> None:
        base = "https://novelarrow.com/novel/shadow-slave"
        previous = ('<section><h3>Latest chapter</h3><a href="/chapter/shadow-slave/'
                    'chapter-entertaining-guest">Chapter Entertaining Guest</a></section>')
        target = previous.replace("entertaining-guest", "freedom-of-choice").replace(
            "Entertaining Guest", "Freedom of Choice")
        waiting = parse_novelarrow_candidates(BeautifulSoup(previous, "html.parser"), base, *CONTEXT)
        released = parse_novelarrow_candidates(BeautifulSoup(target, "html.parser"), base, *CONTEXT)
        self.assertEqual(waiting[0].chapter, 3189)
        self.assertEqual(released[0].chapter, 3190)

    def test_opaque_sequential_list_uses_known_previous(self) -> None:
        base = "https://www.rechapters.com/book/shadow-slave-r2k2ivbd6ez4"
        heading = "<h2>Chapter list</h2><p>Newest first</p>"
        previous = '<a href="/book/shadow-slave-r2k2ivbd6ez4/previous9x">Chapter Entertaining Guest</a>'
        numbered = '<a href="/book/shadow-slave-r2k2ivbd6ez4/numbered8x">Ch 3188: Lost Soul</a>'
        target = '<a href="/book/shadow-slave-r2k2ivbd6ez4/current0xx">Chapter Freedom of Choice</a>'
        self.assert_transition(parse_rechapters_candidates, base,
                               f"<section>{heading}{previous}{numbered}</section>",
                               f"<section>{heading}{target}{previous}{numbered}</section>")

    def test_telegram_sequential_title_only_messages(self) -> None:
        def message(title: str, slug: str) -> str:
            return (f'<div class="tgme_widget_message"><span class="tgme_widget_message_document_title">'
                    f'{title}.docx</span><a href="https://telegra.ph/{slug}">Read</a></div>')
        numbered = message("3188 Lost Soul", "3188-Lost-Soul-01-01")
        previous = message("Entertaining Guest", "Entertaining-Guest-01-02")
        target = message("Freedom of Choice", "Freedom-of-Choice-01-03")
        waiting = parse_telegram_candidates(BeautifulSoup(numbered + previous, "html.parser"),
                                            "https://t.me/s/example", *CONTEXT)
        released = parse_telegram_candidates(BeautifulSoup(numbered + previous + target, "html.parser"),
                                             "https://t.me/s/example", *CONTEXT)
        without_previous = parse_telegram_candidates(
            BeautifulSoup(numbered + previous, "html.parser"), "https://t.me/s/example",
            CONTEXT[0], CONTEXT[1], None, None,
        )
        self.assertEqual(max(r.chapter for r in waiting), 3189)
        self.assertEqual(max(r.chapter for r in released), 3190)
        self.assertNotIn(3189, [r.chapter for r in without_previous])

    def test_telegram_requires_immediate_resolved_predecessor(self) -> None:
        def title_message(title: str, suffix: str) -> str:
            slug = title.replace(" ", "-")
            return (f'<div class="tgme_widget_message"><span class="tgme_widget_message_document_title">'
                    f'{title}.docx</span><a href="https://telegra.ph/{slug}-{suffix}">Read</a></div>')
        def numbered_message(chapter: int, title: str) -> str:
            return (f'<div class="tgme_widget_message"><a href="https://telegra.ph/'
                    f'{chapter}-{title.replace(" ", "-")}-01-01">Read</a></div>')
        previous = title_message("Entertaining Guest", "01-02")
        unknown = title_message("Unknown Arrival", "01-03")
        target = title_message("Freedom of Choice", "01-04")
        cases = (
            (numbered_message(3188, "Lost Soul") + previous + target, True),
            (numbered_message(3188, "Lost Soul") + previous + unknown + target, False),
            (previous + numbered_message(3187, "Older") + target, False),
            (previous + unknown, False),
            (previous + target + unknown, False),
        )
        for html, accepted in cases:
            with self.subTest(accepted=accepted, html=html):
                reports = parse_telegram_candidates(
                    BeautifulSoup(html, "html.parser"), "https://t.me/s/example", *CONTEXT)
                self.assertEqual(3190 in [report.chapter for report in reports], accepted)
                if unknown in html and not target in html:
                    self.assertNotIn(3189, [report.chapter for report in reports])

    def test_numbered_telegram_links_require_canonical_https_urls(self) -> None:
        good = "https://telegra.ph/3189-Entertaining-Guest-01-01"
        self.assertEqual(parse_telegram_telegra_link(good).chapter, 3189)
        invalid = (
            "http://telegra.ph/3189-Entertaining-Guest-01-01",
            "https://telegra.ph:443/3189-Entertaining-Guest-01-01",
            "https://user@telegra.ph/3189-Entertaining-Guest-01-01",
            "https://telegra.ph/3189-Entertaining-Guest-01-01?x=1",
            "https://telegra.ph/3189-Entertaining-Guest-01-01#bad",
            "https://telegra.ph/%33%31%38%39-Entertaining-Guest-01-01",
        )
        target = ('<div class="tgme_widget_message"><span class="tgme_widget_message_document_title">'
                  'Freedom of Choice.docx</span><a href="https://telegra.ph/Freedom-of-Choice-01-02">Read</a></div>')
        for href in invalid:
            with self.subTest(href=href):
                self.assertIsNone(parse_telegram_telegra_link(href))
                predecessor = f'<div class="tgme_widget_message"><a href="{href}">Read</a></div>'
                reports = parse_telegram_candidates(
                    BeautifulSoup(predecessor + target, "html.parser"),
                    "https://t.me/s/example", *CONTEXT)
                self.assertNotIn(3190, [report.chapter for report in reports])

    def test_previous_inference_does_not_require_expected_title(self) -> None:
        context = (3190, None, 3189, "Entertaining Guest")
        cases = (
            (parse_novelfull_candidates, "https://novelfull.com/shadow-slave.html",
             '<section><h2>Latest chapters</h2><a href="/shadow-slave/chapter-entertaining-guest.html">Chapter Entertaining Guest</a><a href="/shadow-slave/chapter-3188-lost-soul.html">Chapter 3188 Lost Soul</a></section>'),
            (parse_freewebnovel_net_candidates, "https://freewebnovel.net/shadow-slave.html",
             '<section><h2>6 Latest Chapters [ Updated now ]</h2><a href="/shadow-slave/chapter-entertaining-guest.html">Chapter Entertaining Guest</a><a href="/shadow-slave/chapter-3188-lost-soul.html">Chapter 3188 Lost Soul</a></section>'),
            (parse_rechapters_candidates, "https://www.rechapters.com/book/shadow-slave-r2k2ivbd6ez4",
             '<section><h2>Chapter list</h2><p>Newest first</p><a href="/book/shadow-slave-r2k2ivbd6ez4/previous9x">Chapter Entertaining Guest</a><a href="/book/shadow-slave-r2k2ivbd6ez4/numbered8x">Ch 3188: Lost Soul</a></section>'),
        )
        for parser, base, html in cases:
            with self.subTest(parser=parser.__name__):
                reports = parser(BeautifulSoup(html, "html.parser"), base, *context)
                self.assertEqual(max(report.chapter for report in reports), 3189)
                self.assertTrue(all(report.chapter is not None for report in reports))
                target_html = html.replace("Entertaining Guest", "Freedom of Choice").replace(
                    "entertaining-guest", "freedom-of-choice").replace("previous9x", "target0xx")
                target_reports = parser(BeautifulSoup(target_html, "html.parser"), base, *context)
                self.assertNotIn(3190, [report.chapter for report in target_reports])

        telegram = ('<div class="tgme_widget_message"><span class="tgme_widget_message_document_title">'
                    'Entertaining Guest.docx</span><a href="https://telegra.ph/Entertaining-Guest-01-02">Read</a></div>')
        reports = parse_telegram_candidates(
            BeautifulSoup(telegram, "html.parser"), "https://t.me/s/example", *context)
        self.assertEqual(max(report.chapter for report in reports), 3189)
        target_telegram = telegram.replace("Entertaining Guest", "Freedom of Choice").replace(
            "Entertaining-Guest", "Freedom-of-Choice")
        target_reports = parse_telegram_candidates(
            BeautifulSoup(target_telegram, "html.parser"), "https://t.me/s/example", *context)
        self.assertNotIn(3190, [report.chapter for report in target_reports])

    def test_missing_previous_context_never_emits_none_chapter(self) -> None:
        html = ('<section><h2>Latest chapters</h2>'
                '<a href="/shadow-slave/chapter-unknown-arrival.html">Chapter Unknown Arrival</a>'
                '<a href="/shadow-slave/chapter-3188-lost-soul.html">Chapter 3188 Lost Soul</a></section>')
        reports = parse_novelfull_candidates(
            BeautifulSoup(html, "html.parser"), "https://novelfull.com/shadow-slave.html",
            3190, None, None, None)
        self.assertTrue(all(report.chapter is not None for report in reports))

    def test_ambiguous_equal_titles_and_non_adjacent_previous_fail_closed(self) -> None:
        html = ('<section><h2>Latest chapters</h2>'
                '<a href="/shadow-slave/chapter-freedom-of-choice.html">Chapter Freedom of Choice</a>'
                '<a href="/shadow-slave/chapter-entertaining-guest.html">Chapter Entertaining Guest</a>'
                '<a href="/shadow-slave/chapter-3188-lost-soul.html">Chapter 3188 Lost Soul</a></section>')
        soup = BeautifulSoup(html, "html.parser")
        reports = parse_novelfull_candidates(soup, "https://novelfull.com/shadow-slave.html",
                                             3190, "Freedom of Choice", 3188, "Entertaining Guest")
        self.assertNotIn(3190, [r.chapter for r in reports])
        ambiguous = parse_novelfull_candidates(soup, "https://novelfull.com/shadow-slave.html",
                                               3190, "Freedom of Choice", 3189, "Freedom of Choice")
        self.assertNotIn(3190, [r.chapter for r in ambiguous])


class RecoveryProbeTests(unittest.TestCase):
    source = SourceConfig("Source", "https://source.example", True, ("source.example",))

    def test_recovery_window_helper_is_deterministic(self) -> None:
        with patch.object(monitor, "utc_now", return_value=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)):
            self.assertTrue(monitor.public_source_recovery_window_open())
        with patch.object(monitor, "utc_now", return_value=datetime(2026, 1, 1, 12, 5, tzinfo=UTC)):
            self.assertFalse(monitor.public_source_recovery_window_open())

    def test_suppressed_probe_success_clears_and_failure_remains_capped(self) -> None:
        report = ChapterReport("Source", 3189, None, "https://source.example/chapter-3189")
        failures = {"Source": 4}
        with patch.object(monitor, "PUBLIC_SITES", (self.source,)), \
             patch.object(monitor, "public_source_recovery_window_open", return_value=True), \
             patch.object(monitor, "check_public_site", return_value=report):
            self.assertEqual(monitor.check_public_sites(RunResult(), failures), [report])
        self.assertEqual(failures, {})
        failures = {"Source": 4}
        with patch.object(monitor, "PUBLIC_SITES", (self.source,)), \
             patch.object(monitor, "public_source_recovery_window_open", return_value=True), \
             patch.object(monitor, "check_public_site", side_effect=RuntimeError("blocked")):
            self.assertEqual(monitor.check_public_sites(RunResult(), failures), [])
        self.assertEqual(failures, {"Source": 4})

    def test_suppressed_skip_does_not_mutate_failures(self) -> None:
        failures = {"Source": 4}
        with patch.object(monitor, "PUBLIC_SITES", (self.source,)), \
             patch.object(monitor, "public_source_recovery_window_open", return_value=False), \
             patch.object(monitor, "check_public_site") as check:
            monitor.check_public_sites(RunResult(), failures)
        check.assert_not_called()
        self.assertEqual(failures, {"Source": 4})

    def test_healthy_previous_report_clears_failure_without_notifying(self) -> None:
        report = ChapterReport("Source", 3189, "Entertaining Guest", "https://source.example/3189")
        failures = {"Source": 3}
        with patch.object(monitor, "PUBLIC_SITES", (self.source,)), \
             patch.object(monitor, "check_public_site", return_value=report):
            reports = monitor.check_public_sites(
                RunResult(), failures, expected_chapter=3190,
                expected_title="Freedom of Choice", previous_chapter=3189,
                previous_title="Entertaining Guest",
            )
        self.assertEqual(reports, [report])
        self.assertEqual(failures, {})
        self.assertIsNone(monitor.aggregate_reports_for_chapter(reports, 3190))
