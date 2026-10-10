"""Minimal synthetic fixtures matching the public responses inspected 2026-10-09.

The unloaded app exposes its metadata and chapter-list GET endpoints. Its
chapter records use integral floats; metadata counts are not chapter evidence.
The navigation source exposes three duplicate title-only Next links, and the
observed destination has no numeric chapter evidence. No tests access websites.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

import requests

from shadow_slave_monitor import monitor, parsers
from shadow_slave_monitor.config import PUBLIC_SITES, PUBLIC_SOURCE_PARSER_REVISION
from shadow_slave_monitor.diagnostics import HtmlDocument, ResponseMetadata, diagnostic_summary
from shadow_slave_monitor.http_client import HttpFetchError, fetch_json
from shadow_slave_monitor.models import ChapterReport, Health, RunResult
from shadow_slave_monitor.notifications import pending_from_report
from shadow_slave_monitor.state_manager import load_state, save_state, validate_state


SOURCE = next(s for s in PUBLIC_SITES if s.name == "Chikari")
NAVIGATION = next(s for s in PUBLIC_SITES if s.name == "LightNovelUp")
IDENTITY_URL = "https://chikari.moe/api/series/shadow-slave"
CHAPTERS_URL = "https://chikari.moe/api/novels/shadow-slave/chapters"
SHELL = ('<html><head><title>chikari.moe</title></head>'
         '<body data-sveltekit-preload-data="hover"><p>chikari didn\'t finish loading.</p>'
         '<script>import("/_app/immutable/entry/start.fixture.js")</script></body></html>')
IDENTITY = {"slug": "shadow-slave", "title": "Shadow Slave", "type": "oel",
            "chapter_count": 9999, "views": 999999, "year": 2026, "latest_number": 9999}


def chapter_page(number=3210, title="Fear of Hope"):
    return {"items": [{"number": float(number), "title": title, "lang": "en",
                       "volume": None, "created_at": "2026-10-09T00:00:00Z"}],
            "total": 9999, "offset": 0, "limit": 100}


def waiting_state():
    return {"latest_seen": 3209, "latest_title": "Previous", "latest_url": SOURCE.url,
            "latest_webnovel": 3210, "latest_webnovel_title": "Fear of Hope",
            "mode": "watch_free_sites", "target_chapter": 3210,
            "target_title": "Fear of Hope", "target_url": "https://www.webnovel.com/book/catalog",
            "pending_notification": None, "public_source_failures": {},
            "public_source_failure_revision": PUBLIC_SOURCE_PARSER_REVISION,
            "source_positions": {}, "updated_at": "2026-10-09T00:00:00+00:00"}


class PublicAPIParserTests(unittest.TestCase):
    def check(self, data=None, identity=None):
        documents = [json.dumps(IDENTITY if identity is None else identity),
                     json.dumps(chapter_page() if data is None else data)]
        with patch.object(parsers, "fetch_html", return_value=SHELL) as html, \
             patch.object(parsers, "fetch_json", side_effect=documents) as api:
            report = parsers.check_public_site(SOURCE)
        html.assert_called_once_with(SOURCE)
        self.assertEqual(api.call_args_list, [call(SOURCE, IDENTITY_URL), call(SOURCE, CHAPTERS_URL)])
        return report

    def test_shell_recovers_using_identity_and_numeric_chapter_record(self):
        report = self.check()
        self.assertEqual((report.source, report.chapter, report.title, report.url),
                         ("Chikari", 3210, "Fear of Hope", "https://chikari.moe/novels/shadow-slave/3210"))
        self.assertIsNone(report.position_url)

    def test_highest_descending_record_and_integer_numbers(self):
        data = chapter_page()
        data["items"] += [{"number": 3209, "title": "Previous", "lang": "en"}]
        self.assertEqual(self.check(data).chapter, 3210)

    def test_empty_listing_and_unrelated_numeric_metadata_do_not_report(self):
        data = chapter_page()
        data["items"] = []
        with self.assertRaisesRegex(parsers.ParseError, "chapter_data_missing"):
            self.check(data)

    def test_wrong_missing_or_ambiguous_identity_fails_before_chapter_request(self):
        for field, value in [("slug", "another-series"), ("title", "Another Series"),
                             ("type", "manga"), ("slug", None), ("title", ["Shadow Slave"])]:
            identity = dict(IDENTITY, **{field: value})
            with self.subTest(field=field, value=value), \
                 patch.object(parsers, "fetch_html", return_value=SHELL), \
                 patch.object(parsers, "fetch_json", return_value=json.dumps(identity)) as api, \
                 self.assertRaisesRegex(parsers.ParseError, "series_identity_mismatch"):
                parsers.check_public_site(SOURCE)
            api.assert_called_once_with(SOURCE, IDENTITY_URL)

    def test_invalid_numbers_are_rejected_without_rounding_or_coercion(self):
        for value in [None, True, False, "3210", 3210.5, 0, -1, 10001, float("nan"), float("inf")]:
            data = chapter_page()
            data["items"][0]["number"] = value
            with self.subTest(value=value), self.assertRaisesRegex(parsers.ParseError, "chapter_data_invalid"):
                self.check(data)

    def test_missing_wrong_language_and_malformed_title_fail_closed(self):
        for field, value in [("number", None), ("lang", None), ("lang", "ru"), ("title", {"chapter": 9999})]:
            data = chapter_page()
            data["items"][0][field] = value
            with self.subTest(field=field), self.assertRaises(parsers.ParseError):
                self.check(data)

    def test_malformed_listings_duplicates_reordering_and_pagination_are_rejected(self):
        cases = [[], {}, {"items": "Chapter 9999"}, chapter_page(), chapter_page(),
                 chapter_page(), chapter_page(), chapter_page(), chapter_page()]
        cases[3]["items"] *= 2
        cases[4]["items"].append({"number": 3211.0, "title": "Later", "lang": "en"})
        cases[5]["items"] *= 101
        cases[6]["offset"] = 100
        cases[7]["offset"] = False
        cases[8]["limit"] = 1
        for data in cases:
            with self.subTest(data=data), self.assertRaises(parsers.ParseError):
                self.check(data)

    def test_malformed_and_duplicate_json_fields_have_controlled_diagnostics(self):
        for document in ["{", '{"slug":"another","slug":"shadow-slave","title":"Shadow Slave","type":"oel"}',
                         '{"private":"token=secret","items":']:
            with self.subTest(document=document), \
                 patch.object(parsers, "fetch_html", return_value=SHELL), \
                 patch.object(parsers, "fetch_json", return_value=HtmlDocument(
                     document, ResponseMetadata(200, "chikari.moe", 1))), \
                 self.assertRaises(parsers.ParseError) as caught:
                parsers.check_public_site(SOURCE)
            summary = diagnostic_summary(SOURCE, caught.exception)
            self.assertIn("code=PARSE_CHAPTER_INVALID", summary)
            self.assertIn("status=200", summary)
            self.assertNotIn("secret", summary)
            self.assertNotIn("private", summary)

    def test_regular_missing_chapters_and_challenge_do_not_trigger_api_requests(self):
        for html in ["<html></html>", "<h1>Chapter 9999</h1>",
                     SHELL + '<title>Just a moment...</title><form id="challenge-form"></form>']:
            with self.subTest(html=html), patch.object(parsers, "fetch_html", return_value=html), \
                 patch.object(parsers, "fetch_json") as api, self.assertRaises(parsers.ParseError):
                parsers.check_public_site(SOURCE)
            api.assert_not_called()

    def test_existing_canonical_html_does_not_trigger_api_requests(self):
        with patch.object(parsers, "fetch_html", side_effect=[
            SHELL + '<a href="/novels/shadow-slave/3210">Latest</a>', '<h1>Chapter 3210 Fear of Hope</h1>']), \
             patch.object(parsers, "fetch_json") as api:
            self.assertEqual(parsers.check_public_site(SOURCE).chapter, 3210)
        api.assert_not_called()

    def test_failed_secondary_request_clears_previous_transport_metadata(self):
        with patch.object(parsers, "fetch_html", return_value=HtmlDocument(SHELL, ResponseMetadata(200, "chikari.moe", 1))), \
             patch.object(parsers, "fetch_json", side_effect=[
                 HtmlDocument(json.dumps(IDENTITY), ResponseMetadata(201, "chikari.moe", 1)),
                 requests.Timeout("private token=secret")]), self.assertRaises(requests.Timeout) as caught:
            parsers.check_public_site(SOURCE)
        summary = diagnostic_summary(SOURCE, caught.exception)
        self.assertNotIn("status=", summary)
        self.assertNotIn("secret", summary)


class PublicAPITransportTests(unittest.TestCase):
    @staticmethod
    def response(content_type="application/json", status=200, body=b"{}"):
        response = Mock(spec=requests.Response)
        response.status_code = status
        response.url = CHAPTERS_URL
        response.is_redirect = status == 302
        response.headers = {"Content-Type": content_type}
        response.encoding = "utf-8"
        response.iter_content.return_value = [body]
        return response

    def test_json_transport_returns_metadata_and_existing_request_bounds(self):
        response = self.response()
        with patch("requests.Session.get", return_value=response) as get:
            document = fetch_json(SOURCE, CHAPTERS_URL)
        self.assertEqual(document, "{}")
        self.assertEqual(document.metadata, ResponseMetadata(200, "chikari.moe", 1))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertTrue(get.call_args.kwargs["stream"])
        response.close.assert_called_once()

    def test_wrong_and_missing_content_types_are_rejected(self):
        for content_type in ["text/html", "text/plain", ""]:
            with self.subTest(content_type=content_type), \
                 patch("requests.Session.get", return_value=self.response(content_type)), \
                 self.assertRaisesRegex(HttpFetchError, "unexpected_content_type"):
                fetch_json(SOURCE, CHAPTERS_URL)

    def test_every_api_redirect_is_rejected_without_fetching_the_destination(self):
        for location in [CHAPTERS_URL, "http://chikari.moe/api/series/shadow-slave",
                         "https://evil.example/secret", "https://user:secret@chikari.moe/api/series/shadow-slave",
                         "/api/series/shadow-slave?token=secret", "/novels/shadow-slave/3210#secret",
                         "/api/series/%73hadow-slave"]:
            response = self.response(status=302)
            response.headers["Location"] = location
            with self.subTest(location=location), patch("requests.Session.get", return_value=response) as get, \
                 self.assertRaises(HttpFetchError) as caught:
                fetch_json(SOURCE, CHAPTERS_URL)
            get.assert_called_once()
            summary = diagnostic_summary(SOURCE, caught.exception)
            self.assertIn("code=HTTP_UNSAFE_REDIRECT", summary)
            self.assertNotIn("secret", summary)

    def test_existing_size_and_retry_bounds_apply_to_json(self):
        with patch("shadow_slave_monitor.http_client.MAX_HTML_BYTES", 2), \
             patch("requests.Session.get", return_value=self.response(body=b"123")), \
             self.assertRaisesRegex(HttpFetchError, "response_too_large"):
            fetch_json(SOURCE, CHAPTERS_URL)
        with patch("requests.Session.get", side_effect=requests.Timeout("private token=secret")) as get, \
             patch("time.sleep"), self.assertRaises(requests.Timeout) as caught:
            fetch_json(SOURCE, CHAPTERS_URL)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(caught.exception.attempts, 3)


class NavigationVerificationTests(unittest.TestCase):
    cursor = {"chapter": 3188, "url": "https://lightnovelup.com/novel/shadow-slave/chapter-3188-lost-soul/"}
    links = '<a class="btn next_page" href="/novel/shadow-slave/chapter-entertaining-guest/">Next</a>' * 3

    def test_observed_title_only_next_stays_unverified_even_with_authoritative_target(self):
        html = '<h1 id="chapter-heading">Shadow Slave - Chapter 3188 Lost Soul</h1>' + self.links
        original = dict(self.cursor)
        with patch.object(parsers, "fetch_html", return_value=html) as fetch, \
             self.assertRaises(parsers.ParseError) as caught:
            parsers.check_public_site(NAVIGATION, self.cursor, 3210, "Fear of Hope", 3209, "Previous")
        self.assertEqual(caught.exception.code, "PARSE_NONCANONICAL_NEXT")
        self.assertEqual(caught.exception.counters["rejected_title_only_chapter_path"], 3)
        self.assertEqual(self.cursor, original)
        fetch.assert_called_once_with(NAVIGATION, self.cursor["url"])

    def test_numbered_cursor_requires_actual_matching_chapter_heading(self):
        for html in ["<html></html>", "<div id='app'></div>",
                     "<h1>Shadow Slave - Chapter Entertaining Guest</h1>",
                     "<h1>Another Series - Chapter 3188 Lost Soul</h1>",
                     "<p>3188 chapters, 2026 views</p>",
                     "<h1>Chapter 3188 Lost Soul</h1><h2>Chapter 3188 Different Title</h2>",
                     "<title>Shadow Slave - Chapter 3187 Previous</title><h1>Chapter 3188 Lost Soul</h1>"]:
            with self.subTest(html=html), patch.object(parsers, "fetch_html", return_value=html), \
                 self.assertRaises(parsers.ParseError):
                parsers.check_lightnovelup(NAVIGATION, self.cursor)


class PublicAPIIntegrationTests(unittest.TestCase):
    def test_stale_api_result_does_not_notify_or_advance_state(self):
        state = waiting_state()
        original = copy.deepcopy(state)
        with patch.object(monitor, "PUBLIC_SITES", (SOURCE,)), \
             patch.object(parsers, "fetch_html", return_value=SHELL), \
             patch.object(parsers, "fetch_json", side_effect=[json.dumps(IDENTITY), json.dumps(chapter_page(3208))]), \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(state, RunResult())
        send.assert_not_called()
        self.assertEqual(state, original)

    def test_api_report_cannot_notify_above_authoritative_target(self):
        state = waiting_state()
        official = ChapterReport("WebNovel", 3210, "Fear of Hope", state["target_url"])
        with patch.object(monitor, "PUBLIC_SITES", (SOURCE,)), \
             patch.object(parsers, "fetch_html", return_value=SHELL), \
             patch.object(parsers, "fetch_json", side_effect=[json.dumps(IDENTITY), json.dumps(chapter_page(3211))]), \
             patch.object(monitor, "check_webnovel", return_value=official) as check, \
             patch.object(monitor, "send_new_chapter") as send:
            monitor.run_watch_free_sites(state, RunResult())
        check.assert_called_once()
        send.assert_not_called()
        self.assertEqual(state["latest_seen"], 3209)

    def test_recovery_success_resets_failures_and_unverified_navigation_preserves_cursor(self):
        positions = {"LightNovelUp": dict(NavigationVerificationTests.cursor)}
        failures = {"Chikari": 4, "LightNovelUp": 4}
        result = RunResult()
        def html(site, url=None):
            return SHELL if site == SOURCE else '<h1>Chapter 3188 Lost Soul</h1>' + NavigationVerificationTests.links
        with patch.object(monitor, "PUBLIC_SITES", (SOURCE, NAVIGATION)), \
             patch.object(monitor, "public_source_recovery_window_open", return_value=True), \
             patch.object(parsers, "fetch_html", side_effect=html), \
             patch.object(parsers, "fetch_json", side_effect=[json.dumps(IDENTITY), json.dumps(chapter_page())]):
            reports = monitor.check_public_sites(result, failures, positions)
        self.assertEqual([r.source for r in reports], ["Chikari"])
        self.assertEqual(failures, {"LightNovelUp": 4})
        self.assertEqual(positions["LightNovelUp"], NavigationVerificationTests.cursor)
        self.assertEqual(result.status, Health.DEGRADED)

    def test_parser_revision_migration_preserves_cursor_and_pending_state(self):
        state = waiting_state()
        state["public_source_failure_revision"] = PUBLIC_SOURCE_PARSER_REVISION - 1
        state["public_source_failures"] = {"Chikari": 4, "LightNovelUp": 4}
        state["source_positions"] = {"LightNovelUp": dict(NavigationVerificationTests.cursor)}
        state["pending_notification"] = pending_from_report(
            3209, ChapterReport("Chikari", 3210, "Fear of Hope", "https://chikari.moe/novels/shadow-slave/3210"))
        migrated = validate_state(state)
        self.assertEqual(migrated["public_source_failures"], {})
        self.assertEqual(migrated["source_positions"]["LightNovelUp"], NavigationVerificationTests.cursor)
        self.assertEqual(migrated["pending_notification"], state["pending_notification"])
        with TemporaryDirectory() as directory, \
             patch("shadow_slave_monitor.state_manager.iso_now", return_value=migrated["updated_at"]):
            path = Path(directory) / "state.json"
            save_state(migrated, path)
            restored, first = load_state(path)
        self.assertFalse(first)
        self.assertEqual(restored, migrated)
