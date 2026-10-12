"""Synthetic fixtures shaped by bounded read-only observations on 2026-10-12.

The public reader detail includes number, series, language, locked and body.
The navigation source advertises /api/v1/novels routes in its API directory;
its first chapter page contains 50 records, including numbered and title-only
slugs. chapter_index is an ordinal, not a chapter number. No live calls here.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

import requests

from shadow_slave_monitor import monitor, parsers
from shadow_slave_monitor.config import HEADERS, PUBLIC_SITES
from shadow_slave_monitor.diagnostics import HtmlDocument, ResponseMetadata, diagnostic_summary
from shadow_slave_monitor.http_client import HttpFetchError, fetch_html
from shadow_slave_monitor.models import ChapterReport, Health, RunResult
from shadow_slave_monitor.notifications import (
    NotificationDeliveryError, notification_body, report_from_pending,
)
from shadow_slave_monitor.state_manager import load_state, save_state, validate_state
import test_public_api_repair as repair
from test_public_api_repair import (
    SOURCE, NAVIGATION, SHELL, IDENTITY, IDENTITY_URL, CHAPTERS_URL,
    chapter_page, reader_detail, waiting_state,
)


NAV_API = "https://lightnovelup.com/api/v1/novels/1883008"
NAV_IDENTITY = {"novel": {"id": 1883008, "title": "Shadow Slave", "permalink": NAVIGATION.url,
                           "totalChapters": 9999, "viewCount": 999999}}
CURSOR = repair.NavigationVerificationTests.cursor
CURSOR_PAGE = ('<h1 id="chapter-heading">Shadow Slave - Chapter 3188 Lost Soul</h1>'
               + repair.NavigationVerificationTests.links)


def nav_record(number=3193, title="Regret Can", slug="chapter-3193-regret-can"):
    return {"chapter_id": "2472131", "post_id": "1883008", "volume_id": "0",
            "chapter_name": f"Chapter {number} {title}", "chapter_slug": slug,
            "chapter_index": "3192", "date": "2026-10-12 00:00:00"}


def nav_listing():
    return {"items": [nav_record(), nav_record(3192, "Miraculous Soul", "chapter-3192-miraculous-soul"),
                      dict(nav_record(), chapter_name="Chapter Entertaining Guest",
                           chapter_slug="chapter-entertaining-guest", chapter_index="3188")]}


def nav_page(number=3193, title="Regret Can", slug="chapter-3193-regret-can"):
    url = NAVIGATION.url + slug + "/"
    return (f'<link rel="canonical" href="{url}">'
            f'<title>Shadow Slave - Chapter {number} {title} - Light Novel Fastest Update</title>'
            f'<h1 id="chapter-heading">Shadow Slave - Chapter {number} {title}</h1>'
            '<div class="reading-content"><div id="chapter-content"><p>Synthetic chapter text.</p></div></div>')


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


class IndexVerificationTests(unittest.TestCase):
    def check(self, listing=None, identity=None, page=None, expected=None):
        with patch.object(parsers, "fetch_html", side_effect=[CURSOR_PAGE,
                           nav_page() if page is None else page]) as html, \
             patch.object(parsers, "fetch_json", side_effect=[json.dumps(NAV_IDENTITY if identity is None else identity),
                           json.dumps(nav_listing() if listing is None else listing)]) as api:
            report = parsers.check_public_site(NAVIGATION, dict(CURSOR), expected, "Authoritative Title")
        self.assertEqual(api.call_args_list, [call(NAVIGATION, NAV_API), call(NAVIGATION, NAV_API + "/chapters")])
        self.assertEqual(html.call_args_list, [call(NAVIGATION, CURSOR["url"]),
                                               call(NAVIGATION, report.url, allow_redirects=False)])
        return report

    def test_observed_index_bypasses_title_only_next_using_independent_numbered_page(self):
        report = self.check()
        self.assertEqual((report.chapter, report.title, report.url, report.position_chapter),
                         (3193, "Regret Can", NAVIGATION.url + "chapter-3193-regret-can/", 3193))
        self.assertEqual(report.strategy, "LightNovelUp:verified_api_record")

    def test_target_is_selected_when_present_instead_of_highest_record(self):
        report = self.check(expected=3192,
                            page=nav_page(3192, "Miraculous Soul", "chapter-3192-miraculous-soul"))
        self.assertEqual((report.chapter, report.title), (3192, "Miraculous Soul"))

    def test_missing_target_returns_verified_stale_report_without_refresh_or_guess(self):
        self.assertEqual(self.check(expected=3211).chapter, 3193)

    def test_unordered_records_are_selected_by_number_and_never_by_position(self):
        listing = nav_listing()
        listing["items"].reverse()
        listing["items"][0]["chapter_index"] = "9999"
        self.assertEqual(self.check(listing).chapter, 3193)

    def test_title_only_record_cannot_match_target_even_with_numeric_index_and_title(self):
        listing = {"items": [dict(nav_record(), chapter_name="Chapter Regret Can",
                                 chapter_slug="chapter-regret-can", chapter_index="3193")]}
        with self.assertRaisesRegex(parsers.ParseError, "chapter_data_missing"):
            self.check(listing, expected=3193)

    def test_wrong_series_identity_is_rejected_before_listing_request(self):
        for field, value in [("id", "1883008"), ("id", 9999), ("title", "Another Series"),
                             ("permalink", NAVIGATION.url + "?token=secret"),
                             ("permalink", "http://lightnovelup.com/novel/shadow-slave/")]:
            identity = copy.deepcopy(NAV_IDENTITY)
            identity["novel"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(parsers.ParseError, "series_identity_mismatch"):
                self.check(identity=identity)

    def test_unexpected_empty_oversized_or_malformed_listings_fail_closed(self):
        for listing in [[], {}, {"items": {}}, {"items": []}, {"items": [None]},
                        {"items": [nav_record()] * 51}, {"items": [dict(nav_record(), post_id="9999")]},
                        {"items": [dict(nav_record(), chapter_name=None)]}]:
            with self.subTest(listing=listing), self.assertRaises(parsers.ParseError):
                self.check(listing)

    def test_unsafe_slugs_are_never_fetched(self):
        for slug in ["chapter-3193-regret-can?token=secret", "chapter-3193-regret-can#secret",
                     "chapter-3193-%72egret-can", "chapter-3193-regret-can/extra",
                     "https://evil.example/novel/shadow-slave/chapter-3193-regret-can",
                     "../another-series/chapter-3193-regret-can"]:
            with self.subTest(slug=slug), self.assertRaisesRegex(parsers.ParseError, "chapter_data_missing"):
                self.check({"items": [dict(nav_record(), chapter_slug=slug)]})

    def test_ambiguous_and_contradictory_numbered_records_are_rejected(self):
        for listing in [
            {"items": [nav_record(), dict(nav_record(), chapter_slug="chapter-3193-different")]},
            {"items": [nav_record()] * 2},
            {"items": [dict(nav_record(), chapter_name="Chapter 3192 Regret Can")]},
            {"items": [dict(nav_record(), chapter_name="3193 chapters")]},
            {"items": [dict(nav_record(), chapter_name="Chapter Regret Can")]},
        ]:
            with self.subTest(listing=listing), self.assertRaises(parsers.ParseError):
                self.check(listing)

    def test_ambiguous_record_diagnostic_identifies_chapter_selection(self):
        with self.assertRaises(parsers.ParseError) as caught:
            self.check({"items": [nav_record(), dict(nav_record(), chapter_slug="chapter-3193-different")]})
        summary = diagnostic_summary(NAVIGATION, caught.exception)
        self.assertIn("code=PARSE_AMBIGUOUS_CHAPTER stage=chapter_validation", summary)
        self.assertNotIn("different", summary)

    def test_http_200_page_requires_series_heading_canonical_readable_body_and_agreement(self):
        good = nav_page()
        pages = ["<html></html>", SHELL, good.replace("Shadow Slave", "Another Series"),
                 good.replace("Chapter 3193", "Chapter 3192"),
                 good.replace('id="chapter-heading"', 'id="other"'),
                 good.replace('rel="canonical"', 'rel="other"'),
                 good.replace('regret-can/"', 'regret-can/?token=secret"'),
                 good.replace("Synthetic chapter text.", " \n"),
                 good + '<h2>Chapter 3192 Different</h2>',
                 good + '<title>Chapter 3192 Different</title>',
                 good + '<h2>Chapter 3193 Different</h2>',
                 good.replace("Regret Can", "Different Title"),
                 good + '<h1 id="chapter-heading">Shadow Slave - Chapter 3193 Regret Can</h1>',
                 good + '<title>Just a moment...</title><form id="challenge-form"></form>']
        for page in pages:
            with self.subTest(page=pages.index(page)), self.assertRaises(parsers.ParseError):
                self.check(page=page)

    def test_verified_target_page_does_not_require_numbered_next_navigation(self):
        self.assertEqual(self.check(expected=3193, page=nav_page() + repair.NavigationVerificationTests.links).chapter, 3193)

    def test_conflicting_cursor_or_ambiguous_next_does_not_trigger_fallback(self):
        for page in ['<h1>Chapter 3187 Previous</h1>' + repair.NavigationVerificationTests.links,
                     '<h1>Chapter 3188 Lost Soul</h1>'
                     '<a href="/novel/shadow-slave/chapter-3189-first/">Next</a>'
                     '<a href="/novel/shadow-slave/chapter-3189-second/">Next</a>']:
            with patch.object(parsers, "fetch_html", return_value=page), \
                 patch.object(parsers, "fetch_json") as api, self.assertRaises(parsers.ParseError):
                parsers.check_public_site(NAVIGATION, CURSOR)
            api.assert_not_called()

    def test_duplicate_malformed_json_and_transport_failure_have_sanitized_metadata(self):
        for document in ["{", '{"novel":{},"novel":{}}',
                         requests.Timeout("private token=secret")]:
            with patch.object(parsers, "fetch_html", return_value=HtmlDocument(
                    CURSOR_PAGE, ResponseMetadata(200, "lightnovelup.com", 1))), \
                 patch.object(parsers, "fetch_json", side_effect=[document]), \
                 self.assertRaises((parsers.ParseError, requests.Timeout)) as caught:
                parsers.check_public_site(NAVIGATION, CURSOR)
            summary = diagnostic_summary(NAVIGATION, caught.exception)
            self.assertNotIn("secret", summary)
            self.assertNotIn("status=", summary)

    def test_selected_page_parse_failure_uses_final_response_metadata(self):
        with patch.object(parsers, "fetch_html", side_effect=[
                HtmlDocument(CURSOR_PAGE, ResponseMetadata(200, "lightnovelup.com", 1)),
                HtmlDocument("<html></html>", ResponseMetadata(202, "lightnovelup.com", 2))]), \
             patch.object(parsers, "fetch_json", side_effect=[json.dumps(NAV_IDENTITY), json.dumps(nav_listing())]), \
             self.assertRaises(parsers.ParseError) as caught:
            parsers.check_public_site(NAVIGATION, CURSOR)
        summary = diagnostic_summary(NAVIGATION, caught.exception)
        self.assertIn("status=202", summary)
        self.assertIn("attempts=2", summary)


class VerifiedPageTransportTests(unittest.TestCase):
    def test_selected_page_rejects_every_redirect_without_fetching_destination(self):
        for location in [NAVIGATION.url + 'chapter-3193-regret-can/',
                         NAVIGATION.url + 'chapter-3193-regret-can/?token=secret',
                         'http://lightnovelup.com/novel/shadow-slave/chapter-3193-regret-can/',
                         'https://evil.example/secret']:
            response = repair.PublicAPITransportTests.response("text/html", 302)
            response.url = NAVIGATION.url + 'chapter-3193-regret-can/'
            response.headers["Location"] = location
            with patch("requests.Session.get", return_value=response) as get, \
                 self.assertRaisesRegex(HttpFetchError, "redirect_not_allowed"):
                fetch_html(NAVIGATION, response.url, allow_redirects=False)
            get.assert_called_once()
            self.assertEqual(get.call_args.kwargs["headers"], HEADERS)
            self.assertEqual(get.call_args.kwargs["timeout"], (5, 20))


class VerifiedSourceIntegrationTests(unittest.TestCase):
    def api(self, site, url):
        documents = {IDENTITY_URL: IDENTITY, CHAPTERS_URL: chapter_page(3210, "Chapter 3210 Fear of Hope"),
                     CHAPTERS_URL + "/3210/read": reader_detail(3210, "Chapter 3210 Fear of Hope"),
                     NAV_API: NAV_IDENTITY, NAV_API + "/chapters": nav_listing()}
        return json.dumps(documents[url])

    def html(self, site, url=None, **kwargs):
        if site == SOURCE:
            return SHELL
        return CURSOR_PAGE if url == CURSOR["url"] else nav_page()

    def test_verified_stale_report_advances_only_source_cursor_and_does_not_notify(self):
        state = waiting_state()
        state["source_positions"] = {"LightNovelUp": dict(CURSOR)}
        with patch.object(monitor, "PUBLIC_SITES", (NAVIGATION,)), \
             patch.object(parsers, "fetch_html", side_effect=self.html), \
             patch.object(parsers, "fetch_json", side_effect=self.api), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(state, RunResult())
        send.assert_not_called()
        self.assertEqual(state["latest_seen"], 3209)
        self.assertEqual(state["target_chapter"], 3210)
        self.assertEqual(state["source_positions"]["LightNovelUp"]["chapter"], 3193)

    def test_index_failure_is_isolated_cursor_preserved_and_primary_title_is_clean(self):
        state = waiting_state()
        state["source_positions"] = {"LightNovelUp": dict(CURSOR)}
        def api(site, url):
            if site == NAVIGATION:
                raise requests.Timeout("private token=secret")
            return self.api(site, url)
        result = RunResult()
        with patch.object(monitor, "PUBLIC_SITES", (NAVIGATION, SOURCE)), \
             patch.object(parsers, "fetch_html", side_effect=self.html), \
             patch.object(parsers, "fetch_json", side_effect=api), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(state, result)
        report = send.call_args.args[2]
        self.assertEqual((report.source, report.chapter, report.title), ("Chikari", 3210, "Fear of Hope"))
        self.assertIn("Latest Chapter: 3210 — Fear of Hope", notification_body(3209, report))
        self.assertNotIn("Chapter 3210 Fear", notification_body(3209, report))
        self.assertEqual(state["source_positions"]["LightNovelUp"], CURSOR)
        self.assertEqual(result.status, Health.DEGRADED)

    def test_unavailable_reader_detail_is_isolated_and_never_notifies(self):
        result = RunResult()
        failures = {}
        def api(site, url):
            if url.endswith("/read"):
                return json.dumps(dict(reader_detail(3210), locked=True))
            return self.api(site, url)
        with patch.object(monitor, "PUBLIC_SITES", (SOURCE, NAVIGATION)), \
             patch.object(parsers, "fetch_html", side_effect=self.html), \
             patch.object(parsers, "fetch_json", side_effect=api):
            reports = monitor.check_public_sites(result, failures, {"LightNovelUp": dict(CURSOR)})
        self.assertEqual([r.source for r in reports], ["LightNovelUp"])
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

    def test_stale_index_cannot_regress_existing_cursor_or_trigger_notification(self):
        state = waiting_state()
        state["source_positions"] = {"LightNovelUp": dict(CURSOR)}
        def api(site, url):
            if url.endswith("/chapters"):
                return json.dumps({"items": [nav_record(3173, "Life Goes On", "chapter-3173-life-goes-on")]})
            return self.api(site, url)
        with patch.object(monitor, "PUBLIC_SITES", (NAVIGATION,)), \
             patch.object(parsers, "fetch_html", side_effect=[
                 CURSOR_PAGE, nav_page(3173, "Life Goes On", "chapter-3173-life-goes-on")]), \
             patch.object(parsers, "fetch_json", side_effect=api), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(state, RunResult())
        send.assert_not_called()
        self.assertEqual(state["source_positions"]["LightNovelUp"], CURSOR)
        self.assertEqual(state["latest_seen"], 3209)

    def test_repeated_stale_success_does_not_persist_operational_state(self):
        state = waiting_state()
        state["source_positions"] = {"LightNovelUp": {"chapter": 3193,
                                       "url": NAVIGATION.url + "chapter-3193-regret-can/"}}
        original = copy.deepcopy(state)
        with patch.object(monitor, "PUBLIC_SITES", (NAVIGATION,)), \
             patch.object(monitor, "load_state", return_value=(state, False)), \
             patch.object(parsers, "fetch_html", return_value=nav_page()), \
             patch.object(parsers, "fetch_json") as api, \
             patch.object(monitor, "save_state") as save, \
             patch.object(monitor, "write_result"), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.main()
        api.assert_not_called()
        save.assert_not_called()
        send.assert_not_called()
        self.assertEqual(state, original)

    def test_source_order_priority_and_pending_retry_survive_normalized_title(self):
        state = waiting_state()
        state["source_positions"] = {"LightNovelUp": dict(CURSOR)}
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
        self.assertEqual(restored["source_positions"]["LightNovelUp"], CURSOR)

    def test_index_target_and_higher_report_enforce_authoritative_target(self):
        for target, official in [(3192, None), (3191, 3191)]:
            state = waiting_state()
            state.update(latest_seen=target - 1, latest_webnovel=target, target_chapter=target,
                         target_title="Miraculous Soul")
            state["source_positions"] = {"LightNovelUp": dict(CURSOR)}
            def html(site, url=None, **kwargs):
                if url == CURSOR["url"]:
                    return CURSOR_PAGE
                if '3192' in url:
                    return nav_page(3192, "Miraculous Soul", "chapter-3192-miraculous-soul")
                return nav_page()
            with self.subTest(target=target), patch.object(monitor, "PUBLIC_SITES", (NAVIGATION,)), \
                 patch.object(parsers, "fetch_html", side_effect=html), \
                 patch.object(parsers, "fetch_json", side_effect=self.api), \
                 patch.object(monitor, "check_webnovel", return_value=ChapterReport(
                     "WebNovel", official or target, "Title", "https://www.webnovel.com/book/catalog")) as check, \
                 patch.object(monitor, "send_new_chapter") as send:
                monitor.run_watch_free_sites(state, RunResult())
            if official is None:
                check.assert_not_called()
                self.assertEqual(send.call_args.args[2].chapter, target)
            else:
                check.assert_called_once()
                send.assert_not_called()
                self.assertEqual(state["latest_seen"], target - 1)
