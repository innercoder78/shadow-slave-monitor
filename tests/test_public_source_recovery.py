from __future__ import annotations

from datetime import UTC, datetime
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from shadow_slave_monitor import monitor
from shadow_slave_monitor.config import PUBLIC_SITES, SourceConfig
from shadow_slave_monitor.models import ChapterReport, RunResult
from shadow_slave_monitor.parsers import (
    parse_freewebnovel_net_candidates,
    parse_novelfull_candidates,
    parse_novelarrow_candidates,
    parse_readnovelfull_candidates,
    parse_rechapters_candidates,
    parse_telegram_candidates,
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
