"""Synthetic fixtures shaped by bounded read-only observations on 2026-10-12.

The public reader detail includes number, series, language, locked and body.
No live calls here.
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

import requests

from shadow_slave_monitor import monitor, parsers
from shadow_slave_monitor.config import PUBLIC_SITES
from shadow_slave_monitor.diagnostics import diagnostic_summary
from shadow_slave_monitor.models import ChapterReport, Health, RunResult
from shadow_slave_monitor.notifications import (
    NotificationDeliveryError, notification_body, report_from_pending,
)
from shadow_slave_monitor.state_manager import load_state, save_state, validate_state
from test_public_api_repair import (
    SOURCE, SHELL, IDENTITY, IDENTITY_URL, CHAPTERS_URL,
    chapter_page, reader_detail, waiting_state,
)


class ReaderVerificationTests(unittest.TestCase):
    def check(self, listing=None, detail=None):
        listing = chapter_page(3211, "Chapter 3211 The Queen's Riddle") if listing is None else listing
        detail = reader_detail(3211, "Chapter 3211 The Queen's Riddle") if detail is None else detail
        with patch.object(parsers, "fetch_html", return_value=SHELL) as html, \
             patch.object(parsers, "fetch_json", side_effect=[json.dumps(IDENTITY), json.dumps(listing),
                                                            json.dumps(detail)]) as api:
            report = parsers.check_public_site(SOURCE)
        html.assert_called_once_with(SOURCE)
        self.assertEqual(api.call_args_list, [call(SOURCE, IDENTITY_URL), call(SOURCE, CHAPTERS_URL),
                                             call(SOURCE, CHAPTERS_URL + "/3211/read")])
        return report

    def test_matching_prefix_removed_without_changing_verified_number_or_url(self):
        report = self.check()
        self.assertEqual((report.chapter, report.title, report.url),
                         (3211, "The Queen's Riddle", "https://chikari.moe/novels/shadow-slave/3211"))
        self.assertEqual(report.strategy, "Chikari:verified_api_record")

    def test_prefix_boundaries_clean_titles_numbers_unicode_and_punctuation(self):
        cases = [
            ("Chapter 3211 The Queen's Riddle", "The Queen's Riddle"),
            ("chapter\u00a03211: Who’s There?! 22 — 第2幕", "Who’s There?! 22 — 第2幕"),
            ("Chapter 3211 — The Queen's Riddle", "The Queen's Riddle"),
            ("Chapter 3211", None),
            ("The Queen's Riddle", "The Queen's Riddle"),
            ("2026: A Memory of 22", "2026: A Memory of 22"),
            ("Chapter 3210 The Queen's Riddle", "Chapter 3210 The Queen's Riddle"),
            ("Chapter 32110 Other", "Chapter 32110 Other"),
            ("Chapter 3211's Riddle", "Chapter 3211's Riddle"),
            ("A Chapter 3211 Riddle", "A Chapter 3211 Riddle"),
            ("Chapter 3211: —A Riddle—", "—A Riddle—"),
            ("-A Riddle:", "-A Riddle:"),
            ("  Chapter 3211   Riddle\x00 22!  ", "Riddle 22!"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(parsers._chikari_api_title(value, 3211), expected)

    def test_absent_malformed_optional_titles_preserve_verified_record(self):
        for value in [None, {}, [], 3211, False, "", " \t"]:
            listing, detail = chapter_page(3211, value), reader_detail(3211, value)
            with self.subTest(value=value):
                report = self.check(listing, detail)
                self.assertEqual((report.chapter, report.title, report.url),
                                 (3211, None, "https://chikari.moe/novels/shadow-slave/3211"))
        listing, detail = chapter_page(3211), reader_detail(3211)
        del listing["items"][0]["title"]
        del detail["title"]
        self.assertIsNone(self.check(listing, detail).title)

    def test_missing_detail_title_keeps_clean_listing_title(self):
        self.assertEqual(self.check(detail=reader_detail(3211, {})).title, "The Queen's Riddle")

    def test_readable_detail_title_can_enrich_missing_listing_title(self):
        self.assertEqual(self.check(listing=chapter_page(3211, None)).title, "The Queen's Riddle")

    def test_only_latest_detail_is_requested_from_full_list(self):
        listing = chapter_page(3211)
        listing["items"] += [{"number": float(n), "title": None, "lang": "en"}
                             for n in range(3210, 3111, -1)]
        self.assertEqual(self.check(listing).chapter, 3211)

    def test_locked_empty_wrong_series_language_and_number_details_fail_closed(self):
        cases = [("locked", True), ("locked", None), ("locked", 0), ("body", ""),
                 ("body", " \n"), ("body", None), ("body", {}), ("novel_slug", "another-series"),
                 ("novel_title", "Another Series"), ("lang", "ru"), ("number", 3210.0),
                 ("number", "3211"), ("number", 3211.5), ("number", True)]
        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(parsers.ParseError):
                self.check(detail=dict(reader_detail(3211), **{field: value}))
        for detail in [[], {}, None, "Chapter 3211"]:
            with self.subTest(detail=detail), self.assertRaises(parsers.ParseError):
                # Explicit None is malformed JSON, rather than the helper's default fixture.
                with patch.object(parsers, "fetch_html", return_value=SHELL), \
                     patch.object(parsers, "fetch_json", side_effect=[json.dumps(IDENTITY),
                                json.dumps(chapter_page(3211)), json.dumps(detail)]):
                    parsers.check_public_site(SOURCE)

    def test_malformed_duplicate_and_unavailable_details_do_not_report(self):
        unavailable = requests.Response()
        unavailable.status_code = 404
        for detail in ["{", '{"number":3210,"number":3211}',
                       requests.HTTPError("private body token=secret", response=unavailable)]:
            with self.subTest(detail=type(detail).__name__), \
                 patch.object(parsers, "fetch_html", return_value=SHELL), \
                 patch.object(parsers, "fetch_json", side_effect=[json.dumps(IDENTITY),
                                json.dumps(chapter_page(3211)), detail]), \
                 self.assertRaises((parsers.ParseError, requests.HTTPError)) as caught:
                parsers.check_public_site(SOURCE)
            self.assertNotIn("secret", diagnostic_summary(SOURCE, caught.exception))


class VerifiedSourceIntegrationTests(unittest.TestCase):
    def api(self, site, url):
        documents = {IDENTITY_URL: IDENTITY, CHAPTERS_URL: chapter_page(3210, "Chapter 3210 Fear of Hope"),
                     CHAPTERS_URL + "/3210/read": reader_detail(3210, "Chapter 3210 Fear of Hope")}
        return json.dumps(documents[url])

    def html(self, site, url=None, **kwargs):
        if site == SOURCE:
            return SHELL
        raise AssertionError("unexpected source")

    def test_peer_failure_preserves_verified_reader_notification_title(self):
        state = waiting_state()
        peer = next(site for site in PUBLIC_SITES if site.name == "NovelFull")
        original_check = monitor.check_public_site
        def check(site, *context):
            if site == peer:
                raise requests.Timeout("private token=secret")
            return original_check(site, *context)
        result = RunResult()
        with patch.object(monitor, "PUBLIC_SITES", (peer, SOURCE)), \
             patch.object(monitor, "check_public_site", side_effect=check), \
             patch.object(parsers, "fetch_html", return_value=SHELL), \
             patch.object(parsers, "fetch_json", side_effect=self.api), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(state, result)
        report = send.call_args.args[2]
        self.assertEqual((report.source, report.chapter, report.title), ("Chikari", 3210, "Fear of Hope"))
        self.assertIn("Latest Chapter: 3210 — Fear of Hope", notification_body(3209, report))
        self.assertNotIn("Chapter 3210 Fear", notification_body(3209, report))
        self.assertEqual(result.status, Health.DEGRADED)


    def test_unavailable_reader_detail_is_isolated_and_never_notifies(self):
        result = RunResult()
        failures = {}
        peer = next(site for site in PUBLIC_SITES if site.name == "NovelFull")
        peer_report = ChapterReport(peer.name, 3193, "Verified peer", peer.url)
        original_check = monitor.check_public_site
        def check(site, *context):
            return peer_report if site == peer else original_check(site, *context)
        def api(site, url):
            if url.endswith("/read"):
                return json.dumps(dict(reader_detail(3210), locked=True))
            return self.api(site, url)
        with patch.object(monitor, "PUBLIC_SITES", (SOURCE, peer)), \
             patch.object(monitor, "check_public_site", side_effect=check), \
             patch.object(parsers, "fetch_html", side_effect=self.html), \
             patch.object(parsers, "fetch_json", side_effect=api):
            reports = monitor.check_public_sites(result, failures)
        self.assertEqual(reports, [peer_report])
        self.assertEqual(failures, {"Chikari": 1})
        self.assertEqual(result.status, Health.DEGRADED)
        state = waiting_state()
        with patch.object(monitor, "PUBLIC_SITES", (SOURCE,)), \
             patch.object(parsers, "fetch_html", return_value=SHELL), \
             patch.object(parsers, "fetch_json", side_effect=api), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(state, RunResult())
        send.assert_not_called()
        self.assertEqual(state["latest_seen"], 3209)
        self.assertIsNone(state["pending_notification"])


    def test_source_order_priority_and_pending_retry_survive_normalized_title(self):
        state = waiting_state()
        telegram = next(s for s in PUBLIC_SITES if s.name == "Telegram")
        original_check = monitor.check_public_site
        def check(site, *args):
            if site == telegram:
                return ChapterReport("Telegram", 3210, "Different optional title", "https://telegra.ph/example")
            return original_check(site, *args)
        with patch.object(monitor, "PUBLIC_SITES", (telegram, SOURCE)), \
             patch.object(monitor, "check_public_site", side_effect=check), \
             patch.object(parsers, "fetch_html", return_value=SHELL), \
             patch.object(parsers, "fetch_json", side_effect=self.api), \
             patch.object(monitor, "send_new_chapter", side_effect=NotificationDeliveryError("http_error", 503)):
            monitor.run_watch_free_sites(state, RunResult())
        pending = state["pending_notification"]
        self.assertEqual((pending["sources"], pending["title"], pending["attempt_count"]),
                         (["Chikari", "Telegram"], "Fear of Hope", 1))
        self.assertEqual(state["latest_seen"], 3209)
        clean = validate_state(state)
        with TemporaryDirectory() as directory, patch("shadow_slave_monitor.state_manager.iso_now",
                                                       return_value=clean["updated_at"]):
            path = Path(directory) / "state.json"
            save_state(clean, path)
            restored, first = load_state(path)
        self.assertFalse(first)
        self.assertEqual(restored, clean)
        with patch.object(monitor, "pending_due", return_value=True), \
             patch.object(monitor, "send_new_chapter") as send, \
             patch.object(monitor, "check_public_sites") as checks:
            monitor.run_watch_free_sites(restored, RunResult())
        checks.assert_not_called()
        previous, report = report_from_pending(pending)
        send.assert_called_once_with(send.call_args.args[0], previous, report)
        self.assertEqual(restored["latest_seen"], 3210)
        self.assertIsNone(restored["pending_notification"])
